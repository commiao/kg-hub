#!/bin/sh
set -eu

if [ "$#" -ne 1 ]; then
  printf '%s\n' 'usage: sh deploy/nas/configure-model-gateway-token.sh /absolute/path/to/caller-token-kg-hub' >&2
  exit 2
fi

script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
"${PYTHON:-python3}" - "$1" "$script_dir/.env.example" "$script_dir/.env" \
  "$script_dir/../../.env" <<'PY'
import os
import re
import secrets
import stat
import sys
import tempfile
from pathlib import Path

source, example, destination, legacy = map(Path, sys.argv[1:])

def read_private(path: Path, limit: int) -> bytes:
    info = path.lstat()
    if (path.is_symlink() or not stat.S_ISREG(info.st_mode)
            or stat.S_IMODE(info.st_mode) != 0o600
            or info.st_uid != os.getuid() or info.st_size > limit):
        raise SystemExit("caller token/.env must be a current-user 0600 regular file")
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        opened = os.fstat(fd)
        if ((opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino)
                or not stat.S_ISREG(opened.st_mode)
                or stat.S_IMODE(opened.st_mode) != 0o600
                or opened.st_uid != os.getuid() or opened.st_size > limit):
            raise SystemExit("private file changed or became unsafe while opening")
        return os.read(fd, limit + 1)
    finally:
        os.close(fd)

try:
    raw = read_private(source, 4096)
except (FileNotFoundError, OSError):
    raise SystemExit("caller token file is missing or unsafe")
try:
    token = raw.decode("ascii").strip()
except UnicodeDecodeError:
    raise SystemExit("caller token file has invalid format")
if (raw not in {token.encode("ascii"), (token + "\n").encode("ascii")}
        or re.fullmatch(r"[A-Za-z0-9_-]{32,256}", token) is None
        or len(set(token)) < 12
        or token.lower().startswith(("replace", "example", "changeme"))):
    raise SystemExit("caller token file has invalid format")

using_legacy = False
using_destination = destination.exists() or destination.is_symlink()
if using_destination:
    try:
        original = read_private(destination, 128 * 1024).decode("utf-8")
    except (OSError, UnicodeError):
        raise SystemExit("deploy/nas/.env is missing, unsafe or invalid UTF-8")
elif legacy.exists() or legacy.is_symlink():
    try:
        original = read_private(legacy, 128 * 1024).decode("utf-8")
        using_legacy = True
    except (OSError, UnicodeError):
        raise SystemExit("legacy root .env is missing, unsafe or invalid UTF-8")
else:
    original = example.read_text("utf-8")

secret_keys = (
    "FALKORDB_PASSWORD",
    "KG_HUB_API_TOKEN",
    "KG_HUB_MODEL_GATEWAY_TOKEN",
)
config_keys = (
    "KG_HUB_DATA_ROOT",
    "ANTHROPIC_BASE_URL",
    "ANTHROPIC_MODEL",
    "MODEL_GATEWAY_PRIVATE_NETWORK",
)
managed_keys = secret_keys + config_keys
line_pattern = re.compile(
    r"^\s*(?:export\s+)?(FALKORDB_PASSWORD|KG_HUB_API_TOKEN|"
    r"KG_HUB_MODEL_GATEWAY_TOKEN|KG_HUB_DATA_ROOT|ANTHROPIC_BASE_URL|"
    r"ANTHROPIC_MODEL|MODEL_GATEWAY_PRIVATE_NETWORK)\s*=(.*)$"
)
provider_secret_pattern = re.compile(
    r"^\s*(?:export\s+)?(?:ANTHROPIC_AUTH_TOKEN|ANTHROPIC_API_KEY|"
    r"DASHSCOPE_API_KEY|OPENAI_API_KEY|QWEN_API_KEY|ALIYUN_API_KEY|"
    r"BAILIAN_API_KEY)\s*=(.*)$"
)

def placeholder(value: str) -> bool:
    lowered = value.strip().lower()
    return (not lowered or lowered.startswith(
        ("replace", "example", "changeme", "placeholder", "<")
    ))

def strong(value: str) -> bool:
    return (re.fullmatch(r"[A-Za-z0-9_-]{32,256}", value) is not None
            and len(set(value)) >= 12 and not placeholder(value))

def legacy_acceptable(value: str) -> bool:
    """Allow an existing working server secret through the one-time migration.

    This does not bless it as strong. It prevents an unrelated password rotation
    from breaking the live FalkorDB while moving provider credentials out of
    kg-hub. Once deploy/nas/.env exists, all subsequent runs remain strict.
    """
    return (8 <= len(value) <= 256 and not placeholder(value)
            and all(0x21 <= ord(ch) <= 0x7e for ch in value))

found = {key: [] for key in managed_keys}
for line in original.splitlines():
    provider_match = provider_secret_pattern.match(line)
    if provider_match:
        provider_value = provider_match.group(1).strip()
        if (len(provider_value) >= 2
                and provider_value[0] == provider_value[-1]
                and provider_value[0] in {'"', "'"}):
            provider_value = provider_value[1:-1]
        if provider_value == token:
            raise SystemExit(
                "gateway caller token must be independent from provider credentials"
            )
    match = line_pattern.match(line)
    if match and match.group(1) in found:
        found[match.group(1)].append(match.group(2))

template = {}
for line in example.read_text("utf-8").splitlines():
    match = line_pattern.match(line)
    if match and match.group(1) in config_keys:
        template[match.group(1)] = match.group(2)
if set(template) != set(config_keys):
    raise SystemExit("deploy/nas/.env.example is missing managed gateway configuration")

selected = {"KG_HUB_MODEL_GATEWAY_TOKEN": token}
for key in config_keys:
    values = found[key]
    distinct = set(values)
    # deploy/nas/.env is operator-owned after first creation. Preserve every
    # explicit non-placeholder routing/data/network value. The template only
    # supplies an absent first-deploy value; reruns must never reset custom NAS
    # paths, gateway origins, business keys or private networks.
    if values and len(distinct) == 1 and not placeholder(values[0]):
        if using_legacy and key in {"ANTHROPIC_BASE_URL", "ANTHROPIC_MODEL"}:
            # A legacy root .env used these names for a direct provider. Drop
            # that provider routing during the one-time gateway migration.
            selected[key] = template[key]
        else:
            selected[key] = values[0]
    elif not values or all(placeholder(value) for value in values):
        selected[key] = template[key]
    else:
        raise SystemExit(f"existing {key} is ambiguous; resolve it before rerun")
generated = set()
preserved_legacy_weak = set()
for key in ("FALKORDB_PASSWORD", "KG_HUB_API_TOKEN"):
    values = found[key]
    if (values and len(set(values)) == 1
            and (strong(values[0])
                 or (using_legacy and legacy_acceptable(values[0])))):
        selected[key] = values[0]
        if not strong(values[0]):
            preserved_legacy_weak.add(key)
    elif not values or all(placeholder(value) for value in values):
        generated.add(key)
    else:
        raise SystemExit(
            f"existing {key} is weak or ambiguous; replace it deliberately before rerun"
        )

used = {token, *(selected[key] for key in secret_keys
                 if key in selected and key != "KG_HUB_MODEL_GATEWAY_TOKEN")}
for key in ("FALKORDB_PASSWORD", "KG_HUB_API_TOKEN"):
    if key not in generated:
        continue
    while True:
        candidate = secrets.token_urlsafe(32)
        if strong(candidate) and candidate not in used:
            selected[key] = candidate
            used.add(candidate)
            break
if len({selected[key] for key in secret_keys}) != len(secret_keys):
    raise SystemExit("managed secrets must be strong and independent")

updated = []
replaced = set()
for line in original.splitlines():
    if provider_secret_pattern.match(line):
        continue
    match = line_pattern.match(line)
    if match:
        key = match.group(1)
        if key not in replaced:
            updated.append(key + "=" + selected[key])
            replaced.add(key)
        continue
    updated.append(line)
for key in managed_keys:
    if key not in replaced:
        updated.append(key + "=" + selected[key])
payload = ("\n".join(updated).rstrip("\n") + "\n").encode("utf-8")

destination.parent.mkdir(parents=True, exist_ok=True)
fd, temporary = tempfile.mkstemp(prefix=".env.tmp.", dir=destination.parent)
try:
    os.fchmod(fd, 0o600)
    with os.fdopen(fd, "wb") as stream:
        stream.write(payload)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, destination)
    directory_fd = os.open(destination.parent, os.O_RDONLY)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)
except BaseException:
    try:
        os.close(fd)
    except OSError:
        pass
    try:
        os.unlink(temporary)
    except FileNotFoundError:
        pass
    raise
print("updated deploy/nas/.env with the kg-hub gateway caller token")
for key in sorted(preserved_legacy_weak):
    print(f"warning: preserved legacy {key}; rotate it in a separate reviewed change")
PY
