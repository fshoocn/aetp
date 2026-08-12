"""Master 命令行入口。

职责：
1. 解析命令行参数（host / port / env-file / reload）
2. 从 .env 加载进程级配置（组合根，唯一初始化点）
3. 启动 uvicorn 加载 FastAPI app

用法:
    python -m master.run
    python -m master.run --host 0.0.0.0 --port 8080 --env-file path/to/.env --reload

打包为 exe 后直接双击运行，默认读取 exe 同目录的 .env。
"""

import argparse
import logging
import uvicorn

from common.logging_config import configure_logging

logger = logging.getLogger(__name__)






def main() -> None:
    """Master CLI 入口：解析参数、加载配置、启动 uvicorn。

    执行顺序：
    1. argparse 解析 --host/--port/--env-file/--reload
    2. configure(args.env_file) 从外置 .env 文件初始化进程级配置
       （组合根模式：此后所有模块通过 get_settings() 只读获取）
    3. 命令行 --host/--port 优先覆盖 .env 默认值
    4. uvicorn.run 启动 FastAPI app，使用 SelectorEventLoop 工厂
       （Windows 默认 Proactor 与 MQTT/子进程不兼容）
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default=None, help="覆盖配置的 http_host（默认 127.0.0.1）")
    parser.add_argument("--port", type=int, default=None, help="覆盖配置的 http_port（默认 8000）")
    parser.add_argument(
        "--env-file",
        default=None,
        help="自定义 .env 配置文件路径（默认 exe/项目根目录下的 .env）",
    )
    parser.add_argument("--reload", action="store_true", help="开发模式自动重载（文件变更时热重启）")
    args = parser.parse_args()

    from master.config import configure

    # 组合根：进程内唯一配置初始化点，此后所有模块用 get_settings() 读取
    settings = configure(args.env_file)
    # 启动时校验安全配置（JWT 密钥强度），弱配置直接拒绝启动
    from master.api.v1.security import validate_security_settings

    validate_security_settings()
    log_path = configure_logging(
        settings.log_file,
        level=settings.log_level,
        console=settings.log_console,
    )
    # 命令行参数优先级高于 .env 文件
    host = args.host or settings.http_host
    port = args.port or settings.http_port
    browser_host = "127.0.0.1" if host in ("0.0.0.0", "::") else host
    logger.info("日志文件: %s", log_path)
    logger.info("Master 监听地址: %s:%s", host, port)
    logger.info("Web 端链接: http://%s:%s/", browser_host, port)
    logger.info("正在启动 Master API")

    try:
        uvicorn.run(
            "master.main:app",
            host=host,
            port=port,
            reload=args.reload,
            # 使用 SelectorEventLoop 工厂，绕开 Windows 默认 Proactor
            loop="master.loop_factory:selector_loop_factory",
        )
    except Exception:
        logger.exception("Master API 启动失败")
        raise



if __name__ == "__main__":
    main()