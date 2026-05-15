#!/usr/bin/env bash
# test-setup-scanner-args.sh — smoke test for scripts/setup-scanner.sh.
#
# Verifies:
#   - shellcheck is clean (warnings produced via `# shellcheck disable` with
#     reason are acceptable).
#   - Arg validation rejects missing/empty arguments with exit 2.
#
# Does NOT exercise az / Graph: those are gated behind precondition checks
# which run after arg parsing.
#
# Step 4 was collapsed from 8 `az ad app permission add` calls to a single
# Graph PATCH on /applications/{id}. End-to-end validation of that change
# requires a real tenant; see the manual VERIFY checklist in setup-scanner.sh.

set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SCRIPT="${HERE}/setup-scanner.sh"

if [[ ! -f "${SCRIPT}" ]]; then
    echo "FAIL: ${SCRIPT} not found." >&2
    exit 1
fi

PASS=0
FAIL=0

assert_eq() {
    local label=$1 expected=$2 actual=$3
    if [[ "${expected}" == "${actual}" ]]; then
        printf '  PASS %s (got %s)\n' "${label}" "${actual}"
        PASS=$((PASS + 1))
    else
        printf '  FAIL %s (expected %s, got %s)\n' "${label}" "${expected}" "${actual}" >&2
        FAIL=$((FAIL + 1))
    fi
}

# ---------------------------------------------------------------------------
# shellcheck
# ---------------------------------------------------------------------------
echo "[shellcheck]"
if command -v shellcheck >/dev/null 2>&1; then
    if shellcheck "${SCRIPT}"; then
        echo "  PASS shellcheck clean"
        PASS=$((PASS + 1))
    else
        echo "  FAIL shellcheck reported issues" >&2
        FAIL=$((FAIL + 1))
    fi
else
    echo "  SKIP shellcheck not on PATH"
fi

# ---------------------------------------------------------------------------
# Arg validation
# ---------------------------------------------------------------------------
echo "[arg validation]"

set +e
bash "${SCRIPT}" >/dev/null 2>&1
RC0=$?
bash "${SCRIPT}" only-one-arg >/dev/null 2>&1
RC1=$?
bash "${SCRIPT}" '' '' >/dev/null 2>&1
RC_EMPTY=$?
bash "${SCRIPT}" 'tenant' '' >/dev/null 2>&1
RC_EMPTY_NAME=$?
bash "${SCRIPT}" 'a' 'b' 'c' >/dev/null 2>&1
RC3=$?
set -e

assert_eq "0 args -> exit 2" 2 "${RC0}"
assert_eq "1 arg  -> exit 2" 2 "${RC1}"
assert_eq "2 empty args -> exit 2" 2 "${RC_EMPTY}"
assert_eq "empty app name -> exit 2" 2 "${RC_EMPTY_NAME}"
assert_eq "3 args -> exit 2" 2 "${RC3}"

# Usage banner printed to stderr on bad invocation.
STDERR=$(bash "${SCRIPT}" 2>&1 >/dev/null || true)
if printf '%s' "${STDERR}" | grep -q 'Usage:'; then
    echo "  PASS usage banner printed on bad invocation"
    PASS=$((PASS + 1))
else
    echo "  FAIL usage banner missing from stderr" >&2
    FAIL=$((FAIL + 1))
fi

# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------
echo
echo "passed: ${PASS}  failed: ${FAIL}"
if [[ ${FAIL} -gt 0 ]]; then
    exit 1
fi
exit 0
