#!/usr/bin/env bash
# 从本机技能目录同步代码到本仓库，并在提交前做敏感信息拦截。
# 用法： bash tools/sync.sh [技能目录]
#   默认技能目录：$HOME/.workbuddy/skills/chatgpt-session-ops
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SRC="${1:-$HOME/.workbuddy/skills/chatgpt-session-ops}"

[ -d "$SRC" ] || { echo "技能目录不存在：$SRC" >&2; exit 1; }

echo "==> 同步：$SRC  ->  $ROOT"
cp "$SRC/SKILL.md" "$ROOT/SKILL.md"
mkdir -p "$ROOT/references" "$ROOT/scripts"
cp "$SRC"/references/*.md "$ROOT/references/" 2>/dev/null || true
cp "$SRC"/scripts/*.py "$ROOT/scripts/"

# 只拷代码，绝不拷凭据与运行态
cd "$ROOT"
git add -A

echo "==> 敏感信息拦截（命中即失败）"
FAIL=0
scan() {
  local label="$1" pattern="$2" allow="${3:-}"
  local hits
  # tools/ 里是本脚本自身，必然包含检测用的正则字面量，跳过
  hits="$( { git grep --cached -nIiE "$pattern" -- . ':!tools' || true; } | { if [ -n "$allow" ]; then grep -vE "$allow" || true; else cat; fi; } )"
  if [ -n "$hits" ]; then
    echo "  [拒绝] $label"
    echo "$hits" | head -10
    FAIL=1
  else
    echo "  [通过] $label"
  fi
}
scan "JWT / access token"  'eyJhbGciOi|eyJ[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{20,}'
scan "邮箱地址"            '[a-z0-9._%+-]+@[a-z0-9.-]+\.(com|cn|net|org)'  'noreply\.github\.com|@users\.noreply'
scan "cookie 实值"         'session-token=[^"'"'"'` ]{10,}|__cf_bm=[^"'"'"'` ]{10,}|oai-did=[0-9a-f]{8}-'
scan "本机绝对路径"        'C:[\\]Users[\\][A-Za-z]|/Users/[A-Za-z]'
# UUID 只放行前端 build id（形如 /c/<uuid>/_next/），其余 UUID 可能是真实会话 id
scan "UUID（疑似会话 id）" '[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}'  '/_next/'

if [ "$FAIL" -ne 0 ]; then
  echo "==> 已拦截，未提交。请先清理上述内容（或在上游技能目录里改掉，别在这里补丁）。" >&2
  exit 1
fi

echo "==> 暂存文件："
git diff --cached --name-only
echo "==> 就绪。确认后执行： git commit -m '...' && git push"
