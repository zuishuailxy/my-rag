#!/usr/bin/env bash
#
# 一键起开发环境：基础设施 → 后端 → 前端
#
# 用途：录 demo / 截图 / 手工验收。跑完会打印三个地址与「截图清单」提示。
#
# 用法：
#   ./scripts/dev_up.sh          # 起全部（基础设施 + 后端 + 前端）
#   ./scripts/dev_up.sh --infra  # 只起基础设施
#
# 停止：脚本退出时用 Ctrl-C，只会关掉后端与前端；基础设施用
#   docker compose down
# 单独停（数据卷保留，下次启动更快）。
#
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BACKEND="$ROOT/backend"
FRONTEND="$ROOT/frontend"
LOG_DIR="$ROOT/.dev_logs"
PIDS=()

info() { printf '\033[1;34m▶\033[0m %s\n' "$1"; }
ok()   { printf '\033[1;32m✓\033[0m %s\n' "$1"; }
warn() { printf '\033[1;33m!\033[0m %s\n' "$1"; }

CLEANED=0
cleanup() {
  # 防重入：INT/TERM 触发一次后，退出时的 EXIT 会再触发一次
  [ "$CLEANED" = "1" ] && return
  CLEANED=1
  echo
  info "关闭后端与前端…"
  for pid in "${PIDS[@]:-}"; do
    [ -n "$pid" ] && kill "$pid" 2>/dev/null || true
  done
  warn "基础设施仍在运行；需要停掉请执行：docker compose down"
}
trap cleanup EXIT INT TERM

# 前端探测一律用 localhost：vite 默认可能只绑 IPv6 [::1]，
# 用 127.0.0.1 探会永远探不到（踩过：明明在跑，却判定「未就绪」）。
front_up() { curl -s -o /dev/null --max-time 3 http://localhost:5173 2>/dev/null; }

# ---------------------------------------------------------------- 基础设施
info "启动基础设施（docker compose）"
docker compose -f "$ROOT/docker-compose.yml" up -d

info "等待关键服务就绪（ES / PostgreSQL / Redis）"
for i in $(seq 1 90); do
  es_ok=$(curl -s -o /dev/null -w '%{http_code}' --max-time 3 http://localhost:9200 2>/dev/null || echo 000)
  pg_ok=$(docker compose -f "$ROOT/docker-compose.yml" exec -T postgres \
            pg_isready -U user -d knowledge_hub >/dev/null 2>&1 && echo yes || echo no)
  if [ "$es_ok" = "200" ] && [ "$pg_ok" = "yes" ]; then
    ok "基础设施就绪"
    break
  fi
  [ "$i" = "90" ] && warn "等待超时，继续启动应用（可稍后重试）"
  sleep 2
done

if [ "${1:-}" = "--infra" ]; then
  ok "只起基础设施，按要求退出"
  exit 0
fi

# 前端依赖：只在缺失时安装，避免每次启动都等
if [ ! -d "$FRONTEND/node_modules" ]; then
  info "安装前端依赖（首次较慢）"
  (cd "$FRONTEND" && npm install)
fi

mkdir -p "$LOG_DIR"

# ------------------------------------------------------------------- 后端
# 已在跑就复用，不重复启动（否则 EADDRINUSE 直接挂）
if curl -s -o /dev/null --max-time 3 http://127.0.0.1:8000/docs 2>/dev/null; then
  ok "后端已在 8000 运行，直接复用（本脚本不会接管它的生命周期）"
else
  info "启动后端（uvicorn --reload，日志 $LOG_DIR/backend.log）"
  (cd "$BACKEND" && exec uv run uvicorn app.main:app --reload --host 0.0.0.0 --port 8000) \
    >"$LOG_DIR/backend.log" 2>&1 &
  PIDS+=($!)

  for i in $(seq 1 60); do
    # ⚠️ 两个坑都踩过，写在这两行旁边：
    # 1) 变量名必须用 ${code} 括起来：后面紧跟全角括号（多字节 UTF-8）时，
    #    bash 会把该字符的字节当成变量名的一部分 → `codeｘ: unbound variable`
    #    （配合 set -u 直接挂）。凡 `$var` 后接中文/全角字符，一律写 ${var}。
    # 2) **不能**用 `|| echo 000` 兜底：curl 的 -w 在连接失败时本就会输出 `000`，
    #    再拼一个就成 `000000`，拿 `!= "000"` 判断会**假报就绪**。
    #    所以直接用 case 匹配 2xx/3xx。
    code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 3 http://127.0.0.1:8000/docs 2>/dev/null)
    case "$code" in
      2*|3*) ok "后端已就绪（HTTP ${code}）"; break ;;
    esac
    [ "$i" = "60" ] && warn "后端未就绪，请看 $LOG_DIR/backend.log"
    sleep 2
  done
fi

# ------------------------------------------------------------------- 前端
if front_up; then
  ok "前端已在 5173 运行，直接复用"
else
  info "启动前端（vite，日志 $LOG_DIR/frontend.log）"
  # --strictPort：端口被占就直接报错，不静默换一个（否则后续探测全盯错端口）
  (cd "$FRONTEND" && exec npm run dev -- --port 5173 --strictPort) \
    >"$LOG_DIR/frontend.log" 2>&1 &
  PIDS+=($!)

  for i in $(seq 1 40); do
    if front_up; then ok "前端已就绪"; break; fi
    if grep -q "Port 5173 is already in use" "$LOG_DIR/frontend.log" 2>/dev/null; then
      warn "5173 已被占用（可能是你另开的 vite）——用 --strictPort 不会静默换端口，请看日志"
      break
    fi
    [ "$i" = "40" ] && warn "前端未就绪，请看 $LOG_DIR/frontend.log"
    sleep 2
  done
fi

cat <<'BANNER'

────────────────────────────────────────────────────────────
  前端      http://localhost:5173
  后端 API  http://localhost:8000/docs
  Langfuse  http://localhost:3000   （需另起：见 README 可观测一节）

  截图清单见 README「📸 演示截图」一节；建议按顺序截：
    1. 登录页            2. 对话（流式 + 思考 + 工具调用 + [n] 引用）
    3. 检索页（混合检索）  4. 知识图谱全景   5. 文档审核流
    6. 评测输出（终端一张就够，证明数字是跑出来的）
────────────────────────────────────────────────────────────
  按 Ctrl-C 退出（只关后端与前端，基础设施保留）
BANNER

wait
