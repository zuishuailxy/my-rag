
#!/bin/bash
# 切换到脚本所在目录，确保 uv 能找到项目 (pyproject.toml)
cd "$(dirname "$0")"

# 首次运行先同步依赖
uv sync

# 启动开发服务
uv run uvicorn app.main:app --reload

# Docker 运行（docker-compose.yml 已配置好 backend 服务）
# cd .. && docker compose up -d backend