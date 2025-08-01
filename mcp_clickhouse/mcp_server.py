import logging
import json
from typing import Optional, List, Any
import concurrent.futures
import atexit
import os

import clickhouse_connect
import chdb.session as chs
from clickhouse_connect.driver.binding import format_query_value
from dotenv import load_dotenv
from fastmcp import FastMCP
from fastmcp.tools import Tool
from fastmcp.prompts import Prompt
from fastmcp.exceptions import ToolError
from dataclasses import dataclass, field, asdict, is_dataclass
from starlette.requests import Request
from starlette.responses import PlainTextResponse

from mcp_clickhouse.mcp_env import get_config, get_chdb_config
from mcp_clickhouse.chdb_prompt import CHDB_PROMPT


@dataclass
class Column:
    database: str
    table: str
    name: str
    column_type: str
    default_kind: Optional[str]
    default_expression: Optional[str]
    comment: Optional[str]


@dataclass
class Table:
    database: str
    name: str
    engine: str
    create_table_query: str
    dependencies_database: str
    dependencies_table: str
    engine_full: str
    sorting_key: str
    primary_key: str
    total_rows: int
    total_bytes: int
    comment: Optional[str] = None
    columns: List[Column] = field(default_factory=list)


MCP_SERVER_NAME = "mcp-clickhouse"

# Configure logging
logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(MCP_SERVER_NAME)

# 在加载配置后设置线程池大小
load_dotenv()
config = get_config()
thread_pool_size = config.thread_pool_size if config.enabled else 10

QUERY_EXECUTOR = concurrent.futures.ThreadPoolExecutor(max_workers=thread_pool_size)
atexit.register(lambda: QUERY_EXECUTOR.shutdown(wait=True))
SELECT_QUERY_TIMEOUT_SECS = 30

logger.info(f"Initialized thread pool with {thread_pool_size} workers")

mcp = FastMCP(
    name=MCP_SERVER_NAME,
    dependencies=[
        "clickhouse-connect",
        "python-dotenv",
        "pip-system-certs",
        "chdb",
    ],
)


def health_check_sync():
    """Synchronous health check for use in thread pool."""
    # Try to create a client connection to verify ClickHouse connectivity
    client = create_clickhouse_client()
    try:
        version = client.server_version
        return f"OK - Connected to ClickHouse {version}"
    finally:
        # 确保连接被正确关闭
        if client and hasattr(client, 'close'):
            try:
                client.close()
            except Exception as e:
                logger.warning(f"Error closing ClickHouse client in health check: {e}")


@mcp.custom_route("/health", methods=["GET"])
async def health_check(request: Request) -> PlainTextResponse:
    """Health check endpoint for monitoring server status.

    Returns OK if the server is running and can connect to ClickHouse.
    """
    try:
        # 使用线程池异步执行健康检查，避免阻塞主线程
        future = QUERY_EXECUTOR.submit(health_check_sync)
        try:
            result = future.result(timeout=10)  # 10秒超时
            return PlainTextResponse(result)
        except concurrent.futures.TimeoutError:
            future.cancel()
            return PlainTextResponse("ERROR - Health check timed out", status_code=503)
    except Exception as e:
        # Return 503 Service Unavailable if we can't connect to ClickHouse
        return PlainTextResponse(f"ERROR - Cannot connect to ClickHouse: {str(e)}", status_code=503)


def result_to_table(query_columns, result) -> List[Table]:
    return [Table(**dict(zip(query_columns, row))) for row in result]


def result_to_column(query_columns, result) -> List[Column]:
    return [Column(**dict(zip(query_columns, row))) for row in result]


def to_json(obj: Any) -> str:
    if is_dataclass(obj):
        return json.dumps(asdict(obj), default=to_json)
    elif isinstance(obj, list):
        return [to_json(item) for item in obj]
    elif isinstance(obj, dict):
        return {key: to_json(value) for key, value in obj.items()}
    return obj


def list_databases_sync():
    """Synchronous implementation of list_databases for use in thread pool."""
    logger.info("Listing all databases")
    client = create_clickhouse_client()
    try:
        result = client.command("SHOW DATABASES")

        # Convert newline-separated string to list and trim whitespace
        if isinstance(result, str):
            databases = [db.strip() for db in result.strip().split("\n")]
        else:
            databases = [result]

        logger.info(f"Found {len(databases)} databases")
        return json.dumps(databases)
    except Exception as e:
        logger.error(f"Error listing databases: {e}")
        raise ToolError(f"Failed to list databases: {str(e)}")
    finally:
        # 确保连接被正确关闭
        if client and hasattr(client, 'close'):
            try:
                client.close()
            except Exception as e:
                logger.warning(f"Error closing ClickHouse client: {e}")


def list_databases():
    """List available ClickHouse databases"""
    logger.info("Submitting list_databases request to thread pool")
    try:
        # 使用线程池异步执行，避免阻塞主线程
        future = QUERY_EXECUTOR.submit(list_databases_sync)
        try:
            result = future.result(timeout=SELECT_QUERY_TIMEOUT_SECS)
            logger.info("list_databases completed successfully")
            return result
        except concurrent.futures.TimeoutError:
            logger.warning(f"list_databases timed out after {SELECT_QUERY_TIMEOUT_SECS} seconds")
            future.cancel()
            raise ToolError(f"List databases operation timed out after {SELECT_QUERY_TIMEOUT_SECS} seconds")
    except ToolError:
        raise
    except Exception as e:
        logger.error(f"Unexpected error in list_databases: {str(e)}")
        raise RuntimeError(f"Unexpected error during list databases operation: {str(e)}")


def list_tables_sync(database: str, like: Optional[str] = None, not_like: Optional[str] = None):
    """Synchronous implementation of list_tables for use in thread pool."""
    logger.info(f"Listing tables in database '{database}'")
    client = create_clickhouse_client()
    try:
        query = f"SELECT database, name, engine, create_table_query, dependencies_database, dependencies_table, engine_full, sorting_key, primary_key, total_rows, total_bytes, comment FROM system.tables WHERE database = {format_query_value(database)}"
        if like:
            query += f" AND name LIKE {format_query_value(like)}"

        if not_like:
            query += f" AND name NOT LIKE {format_query_value(not_like)}"

        # 第一次查询：获取所有表的基本信息
        result = client.query(query)

        # Deserialize result as Table dataclass instances
        tables = result_to_table(result.column_names, result.result_rows)

        if not tables:
            logger.info("No tables found")
            return []

        logger.info(f"Found {len(tables)} tables, fetching column information...")

        # 第二次查询：批量获取所有表的列信息（关键优化！）
        table_names = [table.name for table in tables]
        table_names_str = ','.join(format_query_value(name) for name in table_names)
        
        batch_column_query = f"""
        SELECT database, table, name, type AS column_type, default_kind, default_expression, comment 
        FROM system.columns 
        WHERE database = {format_query_value(database)} 
        AND table IN ({table_names_str})
        ORDER BY database, table, position
        """
        
        logger.info(f"Executing batch column query for {len(tables)} tables")
        column_result = client.query(batch_column_query)
        all_columns = result_to_column(column_result.column_names, column_result.result_rows)
        
        # 将列信息按表名分组
        columns_by_table = {}
        for column in all_columns:
            table_name = column.table
            if table_name not in columns_by_table:
                columns_by_table[table_name] = []
            columns_by_table[table_name].append(column)
        
        # 为每个表分配其对应的列信息
        for table in tables:
            table.columns = columns_by_table.get(table.name, [])

        logger.info(f"Successfully processed {len(tables)} tables with {len(all_columns)} total columns")
        return [asdict(table) for table in tables]
    except Exception as e:
        logger.error(f"Error listing tables in database '{database}': {e}")
        raise ToolError(f"Failed to list tables: {str(e)}")
    finally:
        # 确保连接被正确关闭
        if client and hasattr(client, 'close'):
            try:
                client.close()
            except Exception as e:
                logger.warning(f"Error closing ClickHouse client: {e}")


def list_tables(database: str, like: Optional[str] = None, not_like: Optional[str] = None):
    """List available ClickHouse tables in a database, including schema, comment,
    row count, and column count."""
    logger.info(f"Submitting list_tables request for database '{database}' to thread pool")
    try:
        # 使用线程池异步执行，避免阻塞主线程
        future = QUERY_EXECUTOR.submit(list_tables_sync, database, like, not_like)
        try:
            # 设置更长的超时时间，因为 list_tables 操作比普通查询更复杂
            LIST_TABLES_TIMEOUT_SECS = 120  # 2分钟超时
            result = future.result(timeout=LIST_TABLES_TIMEOUT_SECS)
            logger.info(f"list_tables completed successfully for database '{database}'")
            return result
        except concurrent.futures.TimeoutError:
            logger.warning(f"list_tables timed out after {LIST_TABLES_TIMEOUT_SECS} seconds for database '{database}'")
            future.cancel()
            raise ToolError(f"List tables operation timed out after {LIST_TABLES_TIMEOUT_SECS} seconds")
    except ToolError:
        raise
    except Exception as e:
        logger.error(f"Unexpected error in list_tables: {str(e)}")
        raise RuntimeError(f"Unexpected error during list tables operation: {str(e)}")


def execute_query(query: str):
    client = create_clickhouse_client()
    try:
        read_only = get_readonly_setting(client)
        res = client.query(query, settings={"readonly": read_only})
        logger.info(f"Query returned {len(res.result_rows)} rows")
        return {"columns": res.column_names, "rows": res.result_rows}
    except Exception as err:
        logger.error(f"Error executing query: {err}")
        raise ToolError(f"Query execution failed: {str(err)}")
    finally:
        # 确保连接被正确关闭
        if client and hasattr(client, 'close'):
            try:
                client.close()
            except Exception as e:
                logger.warning(f"Error closing ClickHouse client: {e}")


def run_select_query(query: str):
    """Run a SELECT query in a ClickHouse database"""
    logger.info(f"Executing SELECT query: {query}")
    try:
        future = QUERY_EXECUTOR.submit(execute_query, query)
        try:
            result = future.result(timeout=SELECT_QUERY_TIMEOUT_SECS)
            # Check if we received an error structure from execute_query
            if isinstance(result, dict) and "error" in result:
                logger.warning(f"Query failed: {result['error']}")
                # MCP requires structured responses; string error messages can cause
                # serialization issues leading to BrokenResourceError
                return {
                    "status": "error",
                    "message": f"Query failed: {result['error']}",
                }
            return result
        except concurrent.futures.TimeoutError:
            logger.warning(f"Query timed out after {SELECT_QUERY_TIMEOUT_SECS} seconds: {query}")
            future.cancel()
            raise ToolError(f"Query timed out after {SELECT_QUERY_TIMEOUT_SECS} seconds")
    except ToolError:
        raise
    except Exception as e:
        logger.error(f"Unexpected error in run_select_query: {str(e)}")
        raise RuntimeError(f"Unexpected error during query execution: {str(e)}")


def create_clickhouse_client():
    client_config = get_config().get_client_config()
    logger.info(
        f"Creating ClickHouse client connection to {client_config['host']}:{client_config['port']} "
        f"as {client_config['username']} "
        f"(secure={client_config['secure']}, verify={client_config['verify']}, "
        f"connect_timeout={client_config['connect_timeout']}s, "
        f"send_receive_timeout={client_config['send_receive_timeout']}s)"
    )

    try:
        client = clickhouse_connect.get_client(**client_config)
        # Test the connection
        version = client.server_version
        logger.info(f"Successfully connected to ClickHouse server version {version}")
        return client
    except Exception as e:
        logger.error(f"Failed to connect to ClickHouse: {str(e)}")
        raise


def get_readonly_setting(client) -> str:
    """Get the appropriate readonly setting value to use for queries.

    This function handles potential conflicts between server and client readonly settings:
    - readonly=0: No read-only restrictions
    - readonly=1: Only read queries allowed, settings cannot be changed
    - readonly=2: Only read queries allowed, settings can be changed (except readonly itself)

    If server has readonly=2 and client tries to set readonly=1, it would cause:
    "Setting readonly is unknown or readonly" error

    This function preserves the server's readonly setting unless it's 0, in which case
    we enforce readonly=1 to ensure queries are read-only.

    Args:
        client: ClickHouse client connection

    Returns:
        String value of readonly setting to use
    """
    read_only = client.server_settings.get("readonly")
    if read_only:
        if read_only == "0":
            return "1"  # Force read-only mode if server has it disabled
        else:
            return read_only.value  # Respect server's readonly setting (likely 2)
    else:
        return "1"  # Default to basic read-only mode if setting isn't present


def create_chdb_client():
    """Create a chDB client connection."""
    if not get_chdb_config().enabled:
        raise ValueError("chDB is not enabled. Set CHDB_ENABLED=true to enable it.")
    return _chdb_client


def execute_chdb_query(query: str):
    """Execute a query using chDB client."""
    client = create_chdb_client()
    try:
        res = client.query(query, "JSON")
        if res.has_error():
            error_msg = res.error_message()
            logger.error(f"Error executing chDB query: {error_msg}")
            return {"error": error_msg}

        result_data = res.data()
        if not result_data:
            return []

        result_json = json.loads(result_data)

        return result_json.get("data", [])

    except Exception as err:
        logger.error(f"Error executing chDB query: {err}")
        return {"error": str(err)}


def run_chdb_select_query(query: str):
    """Run SQL in chDB, an in-process ClickHouse engine"""
    logger.info(f"Executing chDB SELECT query: {query}")
    try:
        future = QUERY_EXECUTOR.submit(execute_chdb_query, query)
        try:
            result = future.result(timeout=SELECT_QUERY_TIMEOUT_SECS)
            # Check if we received an error structure from execute_chdb_query
            if isinstance(result, dict) and "error" in result:
                logger.warning(f"chDB query failed: {result['error']}")
                return {
                    "status": "error",
                    "message": f"chDB query failed: {result['error']}",
                }
            return result
        except concurrent.futures.TimeoutError:
            logger.warning(
                f"chDB query timed out after {SELECT_QUERY_TIMEOUT_SECS} seconds: {query}"
            )
            future.cancel()
            return {
                "status": "error",
                "message": f"chDB query timed out after {SELECT_QUERY_TIMEOUT_SECS} seconds",
            }
    except Exception as e:
        logger.error(f"Unexpected error in run_chdb_select_query: {e}")
        return {"status": "error", "message": f"Unexpected error: {e}"}


def chdb_initial_prompt() -> str:
    """This prompt helps users understand how to interact and perform common operations in chDB"""
    return CHDB_PROMPT


def _init_chdb_client():
    """Initialize the global chDB client instance."""
    try:
        if not get_chdb_config().enabled:
            logger.info("chDB is disabled, skipping client initialization")
            return None

        client_config = get_chdb_config().get_client_config()
        data_path = client_config["data_path"]
        logger.info(f"Creating chDB client with data_path={data_path}")
        client = chs.Session(path=data_path)
        logger.info(f"Successfully connected to chDB with data_path={data_path}")
        return client
    except Exception as e:
        logger.error(f"Failed to initialize chDB client: {e}")
        return None


# Register tools based on configuration
if os.getenv("CLICKHOUSE_ENABLED", "true").lower() == "true":
    mcp.add_tool(Tool.from_function(list_databases))
    mcp.add_tool(Tool.from_function(list_tables))
    mcp.add_tool(Tool.from_function(run_select_query))
    logger.info("ClickHouse tools registered")


if os.getenv("CHDB_ENABLED", "false").lower() == "true":
    _chdb_client = _init_chdb_client()
    if _chdb_client:
        atexit.register(lambda: _chdb_client.close())

    mcp.add_tool(Tool.from_function(run_chdb_select_query))
    chdb_prompt = Prompt.from_function(
        chdb_initial_prompt,
        name="chdb_initial_prompt",
        description="This prompt helps users understand how to interact and perform common operations in chDB",
    )
    mcp.add_prompt(chdb_prompt)
    logger.info("chDB tools and prompts registered")
