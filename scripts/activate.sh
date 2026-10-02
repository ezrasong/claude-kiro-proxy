# Source from bash: source scripts/activate.sh
_claude_kiro_caveman="$(type -P caveman)"
_claude_kiro_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
caveman() {
  if [ "${1:-}" = claude ]; then
    shift
    python3 "$_claude_kiro_root/scripts/stack.py" run -- "$@"
  else
    "$_claude_kiro_caveman" "$@"
  fi
}
kiro-account() {
  python3 "$_claude_kiro_root/scripts/stack.py" account "$@"
}
