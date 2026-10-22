#!/usr/bin/env python3
"""
redinstaller.py — install a folder of Windows redistributables into a Steam
Proton prefix with Protontricks, from a friendly terminal UI.

WHAT IT DOES
    1. Lists every Steam game that has a Proton prefix (`protontricks -l`,
       falling back to scanning Steam's library folders + compatdata).
    2. Lets you search / pick a game, then finds the game's install folder
       (Steam appmanifest, or shortcuts.vdf for non-Steam games) and searches
       it for `_CommonRedist`-style folders. Pick one, browse the game folder,
       or type any custom path.
    3. Runs every installer one at a time, with live progress, and prints a
       summary at the end:
         .exe  protontricks-launch --appid <APPID> <file.exe>
         .msi  protontricks -c 'wine msiexec /i <file.msi>' <APPID>

REQUIREMENTS
    * Python 3.9+
    * Textual:        pip install --user textual     (or: pipx inject / a venv)
                      Arch: sudo pacman -S python-textual
    * Protontricks:   native (`protontricks` + `protontricks-launch` on PATH)
                      or Flatpak (com.github.Matoking.protontricks)

USAGE
    python3 redinstaller.py                     # interactive
    python3 redinstaller.py ~/Games/Foo/_CommonRedist --appid 123456
    python3 redinstaller.py --dry-run           # show commands, run nothing

    Options:
      --flatpak / --native   force a Protontricks flavour (default: auto)
      --recursive            include installers in sub-folders by default
      --silent               pre-enable silent flags for known installers
      --verbose              keep Wine debug output (WINEDEBUG unchanged)
      --dry-run              simulate the install loop
      --launch-options [OPTS]  also set the game's Steam launch options
                             (default OPTS: WINEDLLOVERRIDES="OnlineFix64,
                             SteamOverlay64,winmm,dnet,steam_api64=n,b" %command%)
      --replace-launch-options replace existing options instead of merging

LAUNCH OPTIONS
    Written to userdata/<you>/config/localconfig.vdf (Steam games) or
    shortcuts.vdf (non-Steam games), after a timestamped .bak backup. Steam
    rewrites these files when it exits, so it has to be closed first; the tool
    offers to close it for you.

KEYS
    Game list:  type to filter · ↑/↓ move · Enter select · Ctrl+R reload · Esc quit
    Folder:     Enter on a found folder · Ctrl+B browse · or type a custom path
    Browser:    Enter expand · u use folder · Backspace parent folder · ~ home
    Installing: s skip current · c cancel remaining
"""

from __future__ import annotations

import argparse
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import threading
import time
import urllib.parse
from dataclasses import dataclass
from pathlib import Path

try:
    from rich.console import Console
    from rich.markup import escape
    from rich.panel import Panel
    from rich.table import Table
    from rich.text import Text
    from textual import on, work
    from textual.app import App, ComposeResult
    from textual.binding import Binding
    from textual.containers import Horizontal, Vertical, VerticalScroll
    from textual.screen import ModalScreen, Screen
    from textual.widgets import (
        Button,
        DataTable,
        DirectoryTree,
        Footer,
        Input,
        Label,
        LoadingIndicator,
        OptionList,
        ProgressBar,
        RichLog,
        Static,
        Switch,
        Tree,
    )
    from textual.widgets.option_list import Option
except ImportError:
    sys.stderr.write(
        "This tool needs the 'textual' package.\n\n"
        "  pip install --user textual          # any distro\n"
        "  sudo pacman -S python-textual       # Arch\n"
        "  python3 -m venv ~/.venvs/redinstaller && ~/.venvs/redinstaller/bin/pip install textual\n\n"
        "Or use the dependency-free bash version (redinstaller.sh).\n"
    )
    sys.exit(1)


FLATPAK_ID = "com.github.Matoking.protontricks"
INSTALLER_SUFFIXES = {".exe", ".msi"}

# Exit codes that Windows installers use to mean "fine, really".
OK_EXIT_CODES = {
    0: "",
    1638: "newer version already installed",
    1641: "ok, restart requested (ignored)",
    3010: "ok, reboot requested (ignored)",
}

# Silent-install flags for well-known redistributables, matched on file name.
SILENT_RULES: list[tuple[re.Pattern[str], list[str]]] = [
    (re.compile(r"\.msi$", re.I), ["/qn", "/norestart"]),  # any MSI package, via msiexec
    (re.compile(r"^vc_?redist[._-]", re.I), ["/install", "/quiet", "/norestart"]),  # VC++ 2015+
    (re.compile(r"^vcredist", re.I), ["/q"]),  # VC++ 2005-2013
    (re.compile(r"^dxsetup\.exe$", re.I), ["/silent"]),
    (re.compile(r"^(ndp|dotnet|netfx|windowsdesktop-runtime)", re.I), ["/q", "/norestart"]),
    (re.compile(r"^oalinst\.exe$", re.I), ["/s"]),
]

LOGO = (
    "┏━┓┏━╸╺┳┓╻┏┓╻┏━┓╺┳╸┏━┓╻  ╻  ┏━╸┏━┓",
    "┣┳┛┣╸  ┃┃┃┃┗┫┗━┓ ┃ ┣━┫┃  ┃  ┣╸ ┣┳┛",
    "╹┗╸┗━╸╺┻┛╹╹ ╹┗━┛ ╹ ╹ ╹┗━╸┗━╸┗━╸╹┗╸",
)
LOGO_COLORS = ("#7aa2f7", "#9d7cd8", "#bb9af7")


# ───────────────────────────── backend / discovery ─────────────────────────────


@dataclass(frozen=True)
class Backend:
    kind: str  # "native" | "flatpak"
    list_cmd: tuple[str, ...]
    launch_cmd: tuple[str, ...]


@dataclass(frozen=True)
class Game:
    name: str
    appid: str


@dataclass
class Result:
    exe: Path
    status: str  # "ok" | "fail" | "skipped"
    code: int | None
    note: str
    seconds: float


def flatpak_protontricks_installed() -> bool:
    if not shutil.which("flatpak"):
        return False
    proc = subprocess.run(["flatpak", "info", FLATPAK_ID], capture_output=True)
    return proc.returncode == 0


def detect_backend(force: str | None) -> Backend | None:
    native = Backend("native", ("protontricks",), ("protontricks-launch",))
    flatpak = Backend(
        "flatpak",
        ("flatpak", "run", FLATPAK_ID),
        ("flatpak", "run", "--command=protontricks-launch", FLATPAK_ID),
    )
    has_native = bool(shutil.which("protontricks") and shutil.which("protontricks-launch"))
    if force == "native":
        return native if has_native else None
    if force == "flatpak":
        return flatpak if flatpak_protontricks_installed() else None
    if has_native:
        return native
    if flatpak_protontricks_installed():
        return flatpak
    return None


GAME_LINE = re.compile(r"^(?P<name>.+?) \((?P<appid>\d+)\)\s*$")


def list_games_protontricks(backend: Backend) -> tuple[list[Game], list[str]]:
    """Return (games, warnings) parsed from `protontricks -l`."""
    proc = subprocess.run(
        [*backend.list_cmd, "-l"],
        capture_output=True,
        text=True,
        errors="replace",
        timeout=180,
        stdin=subprocess.DEVNULL,
    )
    games: list[Game] = []
    for line in proc.stdout.splitlines():
        if line.startswith("protontricks (") or line.startswith("Found "):
            continue
        m = GAME_LINE.match(line.strip())
        if m:
            games.append(Game(m["name"], m["appid"]))
    warnings = [
        line.split(":", 1)[-1].strip()
        for line in proc.stderr.splitlines()
        if "ERROR" in line or ("WARNING" in line and "does not exist" not in line)
    ]
    return games, warnings


def steam_roots() -> list[Path]:
    home = Path.home()
    candidates = [
        os.environ.get("STEAM_DIR", ""),
        home / ".local/share/Steam",
        home / ".steam/steam",
        home / ".steam/root",
        home / ".var/app/com.valvesoftware.Steam/.local/share/Steam",
        home / "snap/steam/common/.local/share/Steam",
    ]
    seen: dict[Path, None] = {}
    for c in candidates:
        if c and Path(c).joinpath("steamapps").is_dir():
            seen[Path(c).resolve()] = None
    return list(seen)


def steam_libraries() -> list[Path]:
    """Every Steam library folder listed in libraryfolders.vdf (plus the roots)."""
    libraries: dict[Path, None] = {}
    for root in steam_roots():
        libraries[root] = None
        vdf = root / "steamapps/libraryfolders.vdf"
        try:
            for path in re.findall(r'"path"\s+"([^"]+)"', vdf.read_text(errors="replace")):
                p = Path(path.replace("\\\\", "\\"))
                if (p / "steamapps").is_dir():
                    libraries[p.resolve()] = None
        except OSError:
            pass
    return list(libraries)


def scan_steam_libraries() -> list[Game]:
    """Fallback: read appmanifests, keep apps that have a pfx."""
    games: dict[str, Game] = {}
    for lib in steam_libraries():
        steamapps = lib / "steamapps"
        for acf in steamapps.glob("appmanifest_*.acf"):
            appid = acf.stem.removeprefix("appmanifest_")
            if not (steamapps / "compatdata" / appid / "pfx").is_dir():
                continue
            try:
                m = re.search(r'"name"\s+"([^"]+)"', acf.read_text(errors="replace"))
            except OSError:
                continue
            games[appid] = Game(m[1] if m else f"App {appid}", appid)
    return list(games.values())


# Binary VDF (shortcuts.vdf). Nodes are lists of [type, key, value]: type 0 is a
# nested list, 1 a string, anything else raw bytes, so files round-trip exactly.
_VDF_SIZES = {0x02: 4, 0x03: 4, 0x07: 8, 0x0A: 8}


def parse_binary_vdf(data: bytes) -> list:
    pos = 0

    def cstr() -> str:
        nonlocal pos
        end = data.index(b"\0", pos)
        text = data[pos:end].decode("utf-8", "surrogateescape")
        pos = end + 1
        return text

    def node() -> list:
        nonlocal pos
        out: list = []
        while pos < len(data):
            kind = data[pos]
            pos += 1
            if kind == 0x08:
                return out
            key = cstr()
            if kind == 0x00:
                out.append([kind, key, node()])
            elif kind == 0x01:
                out.append([kind, key, cstr()])
            elif kind in _VDF_SIZES:
                out.append([kind, key, data[pos:pos + _VDF_SIZES[kind]]])
                pos += _VDF_SIZES[kind]
            else:
                raise ValueError(f"unknown VDF type {kind:#x}")
        return out

    return node()


def dump_binary_vdf(items: list, root: bool = True) -> bytes:
    out = bytearray()
    for kind, key, value in items:
        out.append(kind)
        out += key.encode("utf-8", "surrogateescape") + b"\0"
        if kind == 0x00:
            out += dump_binary_vdf(value, root=False) + b"\x08"
        elif kind == 0x01:
            out += value.encode("utf-8", "surrogateescape") + b"\0"
        else:
            out += value
    return bytes(out + b"\x08") if root else bytes(out)


def shortcut_entries(items: list) -> list[list]:
    shortcuts = next((v for t, k, v in items if t == 0x00 and k.lower() == "shortcuts"), [])
    return [v for t, _, v in shortcuts if t == 0x00]


def shortcut_fields(entry: list) -> dict:
    return {k.lower(): int.from_bytes(v, "little") if t == 0x02 else v for t, k, v in entry}


def is_shortcut_for(fields: dict, game: Game) -> bool:
    name = game.name.removeprefix("Non-Steam shortcut: ")
    return str(fields.get("appid")) == game.appid or fields.get("appname") == name


def shortcut_dir(game: Game) -> Path | None:
    """Install folder of a non-Steam shortcut, from userdata/*/config/shortcuts.vdf."""
    for root in steam_roots():
        for vdf in root.glob("userdata/*/config/shortcuts.vdf"):
            try:
                entries = shortcut_entries(parse_binary_vdf(vdf.read_bytes()))
            except (OSError, ValueError, IndexError):
                continue
            for entry in entries:
                fields = shortcut_fields(entry)
                if not is_shortcut_for(fields, game):
                    continue
                start = str(fields.get("startdir", "")).strip('"')
                exe = Path(str(fields.get("exe", "")).strip('"'))
                for candidate in (Path(start) if start else None, exe.parent if exe.is_absolute() else None):
                    if candidate and candidate.is_dir():
                        return candidate
    return None


def find_game_dir(game: Game) -> tuple[Path | None, str]:
    """Return (install folder, where it came from)."""
    for lib in steam_libraries():
        acf = lib / "steamapps" / f"appmanifest_{game.appid}.acf"
        try:
            m = re.search(r'"installdir"\s+"([^"]+)"', acf.read_text(errors="replace"))
        except OSError:
            continue
        if m and (lib / "steamapps/common" / m[1]).is_dir():
            return lib / "steamapps/common" / m[1], "Steam install folder"
    d = shortcut_dir(game)
    if d:
        return d, "non-Steam shortcut start folder"
    return None, ""


REDIST_DIR_NAMES = {
    "_commonredist", "commonredist", "_redist", "redist", "redists", "redistributable",
    "redistributables", "_installer", "__installer", "installers", "directx", "vcredist",
}


MAX_LEVELS_UP = 4


def search_upwards(game_dir: Path, levels: int = MAX_LEVELS_UP) -> tuple[list[tuple[Path, int]], Path, int]:
    """Search game_dir, then each parent up to `levels` higher, stopping at the
    first level with a hit. Never searches $HOME itself or anything above it
    (if the game lives there; it'd pick up other games' installers), nor /. Returns (found, last folder searched, levels climbed)."""
    home = Path.home()
    root, up = game_dir, 0
    found = find_redist_dirs(root)
    while not found and up < levels:
        parent = root.parent
        if parent == root or parent == Path("/") or (
            game_dir.is_relative_to(home) and (parent == home or not parent.is_relative_to(home))
        ):
            break
        root, up = parent, up + 1
        found = find_redist_dirs(root)
    return found, root, up


def find_redist_dirs(root: Path, max_depth: int = 5, budget: float = 4.0) -> list[tuple[Path, int]]:
    """Search root for _CommonRedist-like folders. Returns [(folder, exe count)], best first."""
    deadline = time.monotonic() + budget
    found: list[tuple[Path, int]] = []
    stack = [(root, 0)]
    while stack and time.monotonic() < deadline:
        folder, depth = stack.pop()
        try:
            entries = list(os.scandir(folder))
        except OSError:
            continue
        for e in entries:
            try:
                if e.name.startswith(".") or not e.is_dir(follow_symlinks=False):
                    continue
            except OSError:
                continue
            if e.name.lower() in REDIST_DIR_NAMES:
                exes = find_exes(Path(e.path), recursive=True)
                if exes:
                    found.append((Path(e.path), len(exes)))
                continue  # don't report nested matches (e.g. _CommonRedist/vcredist)
            if depth + 1 < max_depth:
                stack.append((Path(e.path), depth + 1))
    found.sort(key=lambda f: ("commonredist" not in f[0].name.lower(), len(f[0].parts), str(f[0]).lower()))
    return found


def discover_games(backend: Backend) -> tuple[list[Game], str, list[str]]:
    """Return (games, source description, warnings)."""
    warnings: list[str] = []
    try:
        games, warnings = list_games_protontricks(backend)
        if games:
            return sorted(games, key=lambda g: g.name.lower()), "protontricks -l", warnings
    except (OSError, subprocess.SubprocessError) as exc:
        warnings.append(f"protontricks -l failed: {exc}")
    games = scan_steam_libraries()
    return sorted(games, key=lambda g: g.name.lower()), "Steam library scan", warnings


# ───────────────────────────── Steam launch options ─────────────────────────────

DEFAULT_LAUNCH_OPTIONS = 'WINEDLLOVERRIDES="OnlineFix64,SteamOverlay64,winmm,dnet,steam_api64=n,b" %command%'
STEAMID64_BASE = 76561197960265728

# Text VDF (localconfig.vdf, loginusers.vdf). Nodes are lists of [key, value];
# values stay raw (still escaped) so untouched entries round-trip exactly.
_VDF_TOKEN = re.compile(r'"((?:[^"\\]|\\.)*)"|([{}])|//[^\n]*|\s+', re.S)


def parse_text_vdf(text: str) -> list:
    stack: list[list] = [[]]
    pending: str | None = None
    for m in _VDF_TOKEN.finditer(text):
        string, brace = m.group(1), m.group(2)
        if string is not None:
            if pending is None:
                pending = string
            else:
                stack[-1].append([pending, string])
                pending = None
        elif brace == "{":
            node: list = []
            stack[-1].append([pending, node])
            stack.append(node)
            pending = None
        elif brace == "}" and len(stack) > 1:
            stack.pop()
    return stack[0]


def dump_text_vdf(node: list, depth: int = 0) -> str:
    tabs = "\t" * depth
    out = []
    for key, value in node:
        if isinstance(value, list):
            out.append(f'{tabs}"{key}"\n{tabs}{{\n{dump_text_vdf(value, depth + 1)}{tabs}}}\n')
        else:
            out.append(f'{tabs}"{key}"\t\t"{value}"\n')
    return "".join(out)


def vdf_child(node: list, key: str, create: bool = False) -> list | None:
    for k, v in node:
        if k.lower() == key.lower() and isinstance(v, list):
            return v
    if not create:
        return None
    new: list = []
    node.append([key, new])
    return new


def read_vdf_text(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="surrogateescape")


@dataclass
class LaunchTarget:
    path: Path  # localconfig.vdf or shortcuts.vdf
    kind: str  # "localconfig" | "shortcuts"
    current: str  # launch options set today ("" if none)


def is_non_steam(game: Game) -> bool:
    return game.name.startswith("Non-Steam shortcut") or int(game.appid) >= 2**31


def active_user_dirs(root: Path) -> list[Path]:
    """userdata/<id> of the logged-in account: loginusers.vdf MostRecent, else the
    account whose localconfig.vdf Steam wrote most recently."""
    try:
        users = vdf_child(parse_text_vdf(read_vdf_text(root / "config/loginusers.vdf")), "users") or []
        for steamid, node in users:
            if isinstance(node, list) and any(k.lower() == "mostrecent" and v == "1" for k, v in node):
                d = root / "userdata" / str(int(steamid) - STEAMID64_BASE)
                if (d / "config/localconfig.vdf").is_file():
                    return [d]
    except (OSError, ValueError):
        pass
    configs = [p for p in root.glob("userdata/*/config/localconfig.vdf") if p.parent.parent.name != "0"]
    return [max(configs, key=lambda p: p.stat().st_mtime).parent.parent] if configs else []


def localconfig_app(tree: list, appid: str, create: bool = False) -> list | None:
    node: list | None = tree
    for key in ("UserLocalConfigStore", "Software", "Valve", "Steam", "apps", appid):
        node = vdf_child(node, key, create=create) if node is not None else None
    return node


def find_launch_targets(game: Game) -> list[LaunchTarget]:
    targets: list[LaunchTarget] = []
    for root in steam_roots():
        if is_non_steam(game):
            for vdf in root.glob("userdata/*/config/shortcuts.vdf"):
                try:
                    entries = shortcut_entries(parse_binary_vdf(vdf.read_bytes()))
                except (OSError, ValueError, IndexError):
                    continue
                for entry in entries:
                    fields = shortcut_fields(entry)
                    if is_shortcut_for(fields, game):
                        targets.append(LaunchTarget(vdf, "shortcuts", str(fields.get("launchoptions", ""))))
        else:
            for user in active_user_dirs(root):
                path = user / "config/localconfig.vdf"
                try:
                    app = localconfig_app(parse_text_vdf(read_vdf_text(path)), game.appid)
                except OSError:
                    continue
                raw = next((v for k, v in app or [] if k.lower() == "launchoptions" and isinstance(v, str)), "")
                targets.append(LaunchTarget(path, "localconfig", re.sub(r"\\(.)", r"\1", raw)))
    return targets


def merge_launch_options(current: str, new: str, replace: bool) -> str:
    """Combine new options with what's already set, keeping the user's extras:
    env vars from `new` go first (replacing same-named ones), the rest of the
    current options are kept, and bare game args stay after %command%."""
    current, new = current.strip(), new.strip()
    if replace or not current or "%command%" not in new:
        return new
    if current == new:
        return current
    prefix, _, suffix = new.partition("%command%")
    for var in re.findall(r"(?:^|\s)(\w+)=", prefix):
        current = re.sub(rf'(?:^|(?<=\s)){re.escape(var)}=(?:"[^"]*"|\S*)\s*', "", current).strip()
    rest = current if "%command%" in current else f"%command% {current}".strip()
    return " ".join(part for part in (prefix.strip(), rest, suffix.strip()) if part)


def write_atomic(path: Path, data: bytes) -> None:
    tmp = path.with_name(path.name + ".redinstaller-tmp")
    tmp.write_bytes(data)
    shutil.copymode(path, tmp)
    os.replace(tmp, path)


def set_launch_options(game: Game, target: LaunchTarget, value: str) -> Path:
    """Write value into target's file (after backing it up). Returns the backup path."""
    stamp = time.strftime("%Y%m%d-%H%M%S")
    backup = target.path.with_name(f"{target.path.name}.redinstaller-{stamp}.bak")
    n = 1
    while backup.exists():  # never overwrite an earlier backup
        n += 1
        backup = target.path.with_name(f"{target.path.name}.redinstaller-{stamp}-{n}.bak")
    shutil.copy2(target.path, backup)
    if target.kind == "localconfig":
        tree = parse_text_vdf(read_vdf_text(target.path))
        app = localconfig_app(tree, game.appid, create=True)
        raw = value.replace("\\", "\\\\").replace('"', '\\"')
        pair = next((kv for kv in app if kv[0].lower() == "launchoptions" and isinstance(kv[1], str)), None)
        if pair:
            pair[1] = raw
        else:
            app.append(["LaunchOptions", raw])
        write_atomic(target.path, dump_text_vdf(tree).encode("utf-8", "surrogateescape"))
    else:
        items = parse_binary_vdf(target.path.read_bytes())
        for entry in shortcut_entries(items):
            if not is_shortcut_for(shortcut_fields(entry), game):
                continue
            field = next((f for f in entry if f[0] == 0x01 and f[1].lower() == "launchoptions"), None)
            if field:
                field[2] = value
            else:
                entry.append([0x01, "LaunchOptions", value])
        write_atomic(target.path, dump_binary_vdf(items))
    return backup


def steam_running() -> bool:
    for comm in Path("/proc").glob("[0-9]*/comm"):
        try:
            if comm.read_text().strip() == "steam":
                return True
        except OSError:
            continue
    return False


def shutdown_steam(timeout: float = 45) -> bool:
    """Ask Steam to exit cleanly (it saves its config on the way out) and wait."""
    if shutil.which("steam"):
        cmd = ["steam", "-shutdown"]
    elif shutil.which("flatpak"):
        cmd = ["flatpak", "run", "com.valvesoftware.Steam", "-shutdown"]
    else:
        return False
    try:
        subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, start_new_session=True)
    except OSError:
        return False
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not steam_running():
            time.sleep(2)  # let it finish flushing files
            return True
        time.sleep(1)
    return False


# ───────────────────────────── helpers ─────────────────────────────


def normalize_path(raw: str) -> Path:
    """Accept quoted, shell-escaped, ~-prefixed or file:// paths."""
    s = raw.strip()
    if len(s) >= 2 and s[0] == s[-1] and s[0] in "'\"":
        s = s[1:-1]
    if s.startswith("file://"):
        s = urllib.parse.unquote(s[len("file://"):])
    s = re.sub(r"\\(.)", r"\1", s)  # drag-and-drop style "My\ Games"
    return Path(os.path.expandvars(os.path.expanduser(s)))


def find_exes(folder: Path, recursive: bool) -> list[Path]:
    exes: list[Path] = []
    try:
        entries = folder.rglob("*") if recursive else folder.iterdir()
        for p in entries:
            try:
                if p.suffix.lower() in INSTALLER_SUFFIXES and p.is_file():
                    exes.append(p)
            except OSError:
                continue
    except OSError:
        pass
    return sorted(exes, key=lambda p: str(p.relative_to(folder)).lower())


def install_command(backend: Backend, appid: str, installer: Path, silent: bool) -> list[str]:
    """Command that runs one installer inside the game's prefix."""
    flags = silent_flags(installer) if silent else []
    if installer.suffix.lower() == ".msi":
        # Wine can't execute an .msi directly; hand it to msiexec in the prefix.
        inner = shlex.join(["wine", "msiexec", "/i", str(installer), *flags])
        return [*backend.list_cmd, "-c", inner, appid]
    return [*backend.launch_cmd, "--appid", appid, str(installer), *flags]


def silent_flags(exe: Path) -> list[str]:
    for pattern, flags in SILENT_RULES:
        if pattern.search(exe.name):
            return flags
    return []


def failure_hint(output_tail: str, backend: Backend) -> str:
    text = output_tail.lower()
    if "steam installation" in text and "not found" in text:
        return "Steam not found; set STEAM_DIR=/path/to/Steam"
    if "proton" in text and ("could not be found" in text or "not found" in text):
        return "Proton not found; launch the game once in Steam first"
    if "no such file" in text or "does not exist" in text:
        if backend.kind == "flatpak":
            return "Flatpak can't see this folder (grant filesystem access)"
        return "file not found"
    if "permission denied" in text:
        return "permission denied"
    if "winetricks" in text and "not found" in text:
        return "winetricks is missing; install it"
    return ""


def fmt_secs(s: float) -> str:
    return f"{s:.0f}s" if s < 60 else f"{int(s // 60)}m{int(s % 60):02d}s"


def install_instructions() -> str:
    return (
        "[b]Protontricks was not found.[/b]\n\n"
        "Install it with one of:\n"
        "  • Arch / Manjaro   [cyan]sudo pacman -S protontricks[/]\n"
        "  • Debian / Ubuntu  [cyan]sudo apt install protontricks[/]\n"
        "  • Fedora           [cyan]sudo dnf install protontricks[/]\n"
        "  • openSUSE         [cyan]sudo zypper install protontricks[/]\n"
        "  • Any distro       [cyan]pipx install protontricks[/]\n"
        f"  • Flatpak          [cyan]flatpak install flathub {FLATPAK_ID}[/]\n\n"
        "Protontricks also needs [b]winetricks[/b] (native installs) and a Steam\n"
        "library containing at least one game launched once with Proton.\n"
        "Then run this tool again."
    )


# ───────────────────────────── widgets ─────────────────────────────


class Banner(Static):
    """Logo + one-line tip shown at the top of every screen."""

    def __init__(self, tip: str, **kwargs) -> None:
        super().__init__(**kwargs)
        self.tip = tip

    def render(self) -> Text:
        t = Text()
        for line, color in zip(LOGO, LOGO_COLORS):
            t.append(line + "\n", style=f"bold {color}")
        t.append("Windows redistributables → Steam Proton prefixes, via Protontricks\n", style="italic #7dcfff")
        t.append("💡 ", style="")
        t.append(self.tip, style="#a9b1d6")
        return t


CSS = """
Screen { background: $background; }
Banner { padding: 1 2 0 2; height: auto; }
.section { padding: 1 2 0 2; text-style: bold; color: $accent; height: auto; }
#filter, #path { margin: 1 2 0 2; }
#games { margin: 1 2 0 2; height: 1fr; }
#games-status, #path-status { padding: 0 2; height: auto; }
#loading { height: 3; }
.card {
    border: round $primary; padding: 0 2; margin: 1 2 0 2; height: auto;
}
.card-title { text-style: bold; color: $accent; }
.row { height: auto; margin: 1 2 0 2; }
.row Label { padding: 1 1 0 1; }
.buttons { height: auto; align-horizontal: right; padding: 1 2; dock: bottom; }
.buttons Button { margin-left: 2; }
#file-list { min-height: 4; margin: 1 2 0 2; border: round $panel-lighten-2; height: 1fr; padding: 0 1; }
#current { padding: 1 2 0 2; height: auto; }
#progress { padding: 0 2; height: auto; }
#progress Bar { width: 1fr; }
#results { margin: 1 2 0 2; height: auto; max-height: 45%; }
#log { margin: 1 2 0 2; border: round $panel-lighten-2; height: 1fr; }
#path-body { height: 1fr; }
#found { margin: 1 2 0 2; height: auto; max-height: 12; }
#searching { height: 3; }
BrowseScreen { align: center middle; }
#browse-box { width: 90%; height: 85%; border: thick $primary; background: $surface; padding: 0 2; }
#browse-box #tree { height: 1fr; margin-top: 1; }
#browse-box .buttons { dock: none; padding: 1 0 0 0; }
#launch-card Input { margin: 1 0 0 0; }
#launch-card .row { margin: 0; }
#launch-preview { padding: 1 0 0 0; height: auto; }
SteamRunningScreen { align: center middle; }
#steam-box { width: 80; height: auto; border: thick $warning; background: $surface; padding: 1 2; }
#steam-box .buttons { dock: none; padding: 1 0 0 0; }
#steam-status { padding: 1 0 0 0; height: auto; }
#error-box { margin: 1 2; padding: 1 2; border: heavy $error; height: auto; }
.hidden { display: none; }
"""


# ───────────────────────────── screens ─────────────────────────────


class ErrorScreen(Screen):
    BINDINGS = [Binding("escape,q", "app.quit", "Quit")]

    def __init__(self, message: str) -> None:
        super().__init__()
        self.message = message

    def compose(self) -> ComposeResult:
        yield Banner("Fix the problem below, then run the tool again.")
        yield Static(self.message, id="error-box")
        with Horizontal(classes="buttons"):
            yield Button("Quit", variant="error", id="quit")
        yield Footer()

    @on(Button.Pressed, "#quit")
    def _quit(self) -> None:
        self.app.exit()


class GameScreen(Screen):
    BINDINGS = [
        Binding("escape", "app.quit", "Quit"),
        Binding("ctrl+r", "reload", "Reload list"),
        Binding("ctrl+f", "focus_filter", "Search"),
        Binding("down", "focus_table", "List", show=False),
    ]

    def __init__(self) -> None:
        super().__init__()
        self.games: list[Game] = []
        self.shown: list[Game] = []

    def compose(self) -> ComposeResult:
        yield Banner("Type to filter by name or AppID · ↑/↓ to move · Enter to pick the game.")
        yield Input(placeholder="🔎  Filter games…", id="filter")
        yield LoadingIndicator(id="loading")
        yield DataTable(id="games", cursor_type="row", zebra_stripes=True)
        yield Static("", id="games-status")
        yield Footer()

    def on_mount(self) -> None:
        table = self.query_one(DataTable)
        table.add_column("Game", key="name")
        table.add_column("AppID", key="appid")
        self.query_one("#filter", Input).focus()
        self.action_reload()

    def action_reload(self) -> None:
        self.query_one("#loading").display = True
        self.query_one("#games-status", Static).update("[dim]Asking Protontricks for your games…[/]")
        self.load_games()

    @work(thread=True, exclusive=True)
    def load_games(self) -> None:
        games, source, warnings = discover_games(self.app.backend)
        self.app.call_from_thread(self.populate, games, source, warnings)

    def populate(self, games: list[Game], source: str, warnings: list[str]) -> None:
        self.query_one("#loading").display = False
        self.games = games
        status = self.query_one("#games-status", Static)
        if not games:
            msg = (
                "[b red]No games with a Proton prefix were found.[/]\n"
                "Launch the game once in Steam with Proton enabled (Properties → "
                "Compatibility), then press [b]Ctrl+R[/]."
            )
            if warnings:
                msg += "\n[yellow]" + escape("\n".join(warnings[:4])) + "[/]"
            status.update(msg)
            return
        status.update(f"[green]✔[/] {len(games)} games found via [b]{source}[/] · backend: [b]{self.app.backend.kind}[/]")
        self.refilter()
        wanted = self.app.args.appid
        if wanted:
            match = next((g for g in games if g.appid == wanted), None)
            if match:
                self.app.args.appid = None  # only auto-select once
                self.choose(match)
            else:
                self.notify(f"AppID {wanted} has no Proton prefix", severity="warning")

    @on(Input.Changed, "#filter")
    def refilter(self) -> None:
        terms = self.query_one("#filter", Input).value.lower().split()
        self.shown = [g for g in self.games if all(t in f"{g.name} {g.appid}".lower() for t in terms)]
        table = self.query_one(DataTable)
        table.clear()
        for g in self.shown:
            name = Text(g.name, style="dim italic" if g.name.startswith("Non-Steam shortcut") else "")
            table.add_row(name, Text(g.appid, style="#7dcfff"), key=g.appid)

    @on(Input.Submitted, "#filter")
    def submit_filter(self) -> None:
        table = self.query_one(DataTable)
        if self.shown:
            self.choose(self.shown[min(table.cursor_row, len(self.shown) - 1)])

    @on(DataTable.RowSelected, "#games")
    def row_selected(self, event: DataTable.RowSelected) -> None:
        self.choose(next(g for g in self.shown if g.appid == event.row_key.value))

    def on_key(self, event) -> None:
        # Typing while the table has focus jumps back into the filter box.
        if self.focused is self.query_one(DataTable) and event.character and event.is_printable:
            if event.character != " ":
                box = self.query_one("#filter", Input)
                box.focus()
                box.insert_text_at_cursor(event.character)
                event.stop()

    def action_focus_filter(self) -> None:
        self.query_one("#filter", Input).focus()

    def action_focus_table(self) -> None:
        table = self.query_one(DataTable)
        table.focus()
        if table.row_count > 1:
            table.move_cursor(row=table.cursor_row + 1)

    def choose(self, game: Game) -> None:
        self.app.game = game
        self.app.push_screen(PathScreen())


class FolderTree(DirectoryTree):
    """Directory tree showing folders and installers only (no dot-folders)."""

    def filter_paths(self, paths):
        return [
            p for p in paths
            if not p.name.startswith(".") and (p.is_dir() or p.suffix.lower() in INSTALLER_SUFFIXES)
        ]


class BrowseScreen(ModalScreen):
    """Pick a folder by browsing, starting at the game's install folder."""

    BINDINGS = [
        Binding("escape", "cancel", "Cancel"),
        Binding("u", "use", "Use folder"),
        Binding("backspace", "up", "Parent folder"),
        Binding("~", "home", "Home"),
    ]

    def __init__(self, root: Path) -> None:
        super().__init__()
        self.root = root

    def compose(self) -> ComposeResult:
        with Vertical(id="browse-box"):
            yield Static("📂 Browse for the redistributables folder", classes="card-title")
            yield Static("[dim]Enter expands · [b]u[/b] uses the highlighted folder · Backspace goes up · ~ jumps home[/]")
            yield FolderTree(self.root, id="tree")
            yield Static("", id="browse-sel")
            with Horizontal(classes="buttons"):
                yield Button("Cancel", id="browse-cancel")
                yield Button("Use this folder", variant="primary", id="browse-use")
        yield Footer()

    def on_mount(self) -> None:
        self.query_one("#tree").focus()

    def highlighted_dir(self) -> Path:
        tree = self.query_one("#tree", FolderTree)
        node = tree.cursor_node
        if node is None or node.data is None:
            return Path(tree.path)
        path = Path(node.data.path)
        return path if path.is_dir() else path.parent

    @on(Tree.NodeHighlighted)
    def _highlighted(self) -> None:
        folder = self.highlighted_dir()
        n = len(find_exes(folder, recursive=False))
        count = f"[green]{n} installer(s) here[/]" if n else "[dim]no .exe/.msi at this level[/]"
        self.query_one("#browse-sel", Static).update(f"[b]Selected:[/] {escape(str(folder))}  {count}")

    @on(DirectoryTree.FileSelected)
    def file_picked(self, event: DirectoryTree.FileSelected) -> None:
        self.dismiss(Path(event.path).parent)

    @on(Button.Pressed, "#browse-use")
    def action_use(self) -> None:
        self.dismiss(self.highlighted_dir())

    @on(Button.Pressed, "#browse-cancel")
    def action_cancel(self) -> None:
        self.dismiss(None)

    def action_up(self) -> None:
        tree = self.query_one("#tree", FolderTree)
        tree.path = Path(tree.path).parent

    def action_home(self) -> None:
        self.query_one("#tree", FolderTree).path = Path.home()


class PathScreen(Screen):
    BINDINGS = [
        Binding("escape", "app.pop_screen", "Back"),
        Binding("ctrl+b", "browse", "Browse folders"),
    ]

    def __init__(self) -> None:
        super().__init__()
        self.game_dir: Path | None = None
        self.found: list[tuple[Path, int]] = []

    def compose(self) -> ComposeResult:
        g = self.app.game
        yield Banner("Pick a detected redist folder, browse the game folder (Ctrl+B), or type a custom path.")
        with VerticalScroll(id="path-body"):
            with Vertical(classes="card"):
                yield Static("Selected game", classes="card-title")
                yield Static(f"{escape(g.name)}  [#7dcfff]({g.appid})[/]")
                yield Static("[dim]Locating install folder…[/]", id="gamedir")
            yield Static("Redistributable folders in the game", classes="section")
            yield LoadingIndicator(id="searching")
            yield OptionList(id="found")
            yield Static("…or enter a custom path", classes="section")
            yield Input(
                value=self.app.args.folder or "",
                placeholder="/path/to/Game/_CommonRedist",
                id="path",
            )
            with Horizontal(classes="row"):
                yield Switch(value=self.app.args.recursive, id="recursive")
                yield Label("Include sub-folders (most _CommonRedist folders nest installers)")
            yield Static("", id="path-status")
        with Horizontal(classes="buttons"):
            yield Button("Back", id="back")
            yield Button("Browse…", id="browse")
            yield Button("Continue →", variant="primary", id="next")
        yield Footer()

    def on_mount(self) -> None:
        self.query_one("#found").display = False
        self.query_one("#path", Input).focus()
        self.check(scan=False)
        self.locate()

    @work(thread=True, exclusive=True, group="locate")
    def locate(self) -> None:
        game_dir, source = find_game_dir(self.app.game)
        found, root, up = [], game_dir, 0
        if game_dir and source.startswith("non-Steam"):
            # Shortcut start folders are often a sub-folder (bin/, Binaries/…) or an
            # installer next to the game, so look a few levels higher as well.
            found, root, up = search_upwards(game_dir)
        elif game_dir:
            found = find_redist_dirs(game_dir)
        self.app.call_from_thread(self.located, game_dir, source, found, root, up)

    def located(
        self, game_dir: Path | None, source: str, found: list[tuple[Path, int]], root: Path | None, up: int
    ) -> None:
        self.game_dir, self.found = game_dir, found
        self.query_one("#searching").display = False
        info = self.query_one("#gamedir", Static)
        if game_dir:
            info.update(f"[b]Folder[/]  {escape(str(game_dir))}  [dim]({source})[/]")
        else:
            info.update("[yellow]Couldn't locate the install folder.[/] [dim]Browse or type a path below.[/]")
        options = self.query_one("#found", OptionList)
        options.clear_options()
        if found and up:
            info.update(
                f"[b]Folder[/]  {escape(str(game_dir))}  [dim]({source})[/]\n"
                f"[yellow]Nothing inside the game folder; found {len(found)} folder(s) "
                f"{up} level{'s' if up > 1 else ''} up in[/] {escape(str(root))}"
            )
        for i, (folder, n) in enumerate(found):
            shown = folder.relative_to(root) if root and folder.is_relative_to(root) else folder
            options.add_option(Option(
                Text.assemble(("📦 ", ""), (str(shown), "bold"), (f"   {n} installer(s)", "green")), id=f"found-{i}"
            ))
        if not found and game_dir:
            if source.startswith("non-Steam"):
                msg = (
                    f"✘ No _CommonRedist-style folder in the game folder or up to {up} level(s) above it "
                    f"(searched up to {root}). Browse or enter a custom path."
                )
            else:
                msg = "✘ No _CommonRedist-style folder in the game folder. Browse or enter a custom path."
            options.add_option(Option(Text(msg, style="bold red"), disabled=True))
            self.notify("No redistributables folder found", severity="error")
        browse_label = "📂 Browse the game folder…" if game_dir else "📂 Browse your home folder…"
        options.add_option(Option(Text(browse_label, style="#7dcfff"), id="browse"))
        options.display = True
        if found and not self.query_one("#path", Input).value:
            options.highlighted = 0
            options.focus()

    @on(OptionList.OptionSelected, "#found")
    def _picked(self, event: OptionList.OptionSelected) -> None:
        if event.option_id == "browse":
            self.action_browse()
        elif event.option_id and event.option_id.startswith("found-"):
            folder, _ = self.found[int(event.option_id.removeprefix("found-"))]
            self.use_folder(folder)

    @on(Button.Pressed, "#browse")
    def action_browse(self) -> None:
        typed = normalize_path(self.query_one("#path", Input).value) if self.query_one("#path", Input).value.strip() else None
        root = typed if typed and typed.is_dir() else self.game_dir or Path.home()
        self.app.push_screen(BrowseScreen(root), self._browsed)

    def _browsed(self, folder: Path | None) -> None:
        if folder:
            self.use_folder(folder)

    def use_folder(self, folder: Path) -> None:
        """Fill the path box, turn on sub-folder search and go straight to scanning."""
        with self.prevent(Input.Changed, Switch.Changed):
            self.query_one("#path", Input).value = str(folder)
            self.query_one("#recursive", Switch).value = True
        self.check(scan=True)

    @on(Input.Changed, "#path")
    @on(Switch.Changed, "#recursive")
    def _changed(self) -> None:
        self.check(scan=False)

    @on(Input.Submitted, "#path")
    @on(Button.Pressed, "#next")
    def _submit(self) -> None:
        self.check(scan=True)

    @on(Button.Pressed, "#back")
    def _back(self) -> None:
        self.app.pop_screen()

    def check(self, scan: bool) -> None:
        raw = self.query_one("#path", Input).value
        status = self.query_one("#path-status", Static)
        if not raw.strip():
            status.update("[dim]Waiting for a path…[/]")
            return
        folder = normalize_path(raw)
        if folder.is_file() and folder.suffix.lower() in INSTALLER_SUFFIXES:
            folder = folder.parent
        if not folder.exists():
            status.update(f"[red]✘ Not found:[/] {escape(str(folder))}")
            return
        if not folder.is_dir():
            status.update("[red]✘ That's a file, not a folder.[/]")
            return
        if not os.access(folder, os.R_OK | os.X_OK):
            status.update(
                "[red]✘ Permission denied.[/] Fix with e.g. "
                f"[cyan]chmod -R u+rX {escape(shlex.quote(str(folder)))}[/]"
            )
            return
        if not scan:
            status.update("[green]✔ Folder exists.[/] Press [b]Enter[/] to scan for .exe/.msi files.")
            return
        recursive = self.query_one("#recursive", Switch).value
        status.update("[dim]Scanning…[/]")
        self.scan(folder.resolve(), recursive)

    @work(thread=True, exclusive=True, group="scan")
    def scan(self, folder: Path, recursive: bool) -> None:
        exes = find_exes(folder, recursive)
        self.app.call_from_thread(self.scanned, folder, recursive, exes)

    def scanned(self, folder: Path, recursive: bool, exes: list[Path]) -> None:
        status = self.query_one("#path-status", Static)
        if not exes:
            hint = "" if recursive else " Try enabling [b]Include sub-folders[/]."
            status.update(f"[red]✘ No .exe or .msi files in that folder.[/]{hint}")
            return
        unreadable = [p for p in exes if not os.access(p, os.R_OK)]
        if unreadable:
            status.update(f"[red]✘ {len(unreadable)} file(s) aren't readable, e.g.[/] {escape(unreadable[0].name)}")
            return
        status.update(f"[green]✔ Found {len(exes)} installer(s).[/]")
        self.app.folder, self.app.exes = folder, exes
        self.app.push_screen(ConfirmScreen())


class ConfirmScreen(Screen):
    targets: list[LaunchTarget] = []
    plan: list[tuple[LaunchTarget, str]] = []

    BINDINGS = [
        Binding("escape", "app.pop_screen", "Back"),
        Binding("i", "install", "Install"),
    ]

    def compose(self) -> ComposeResult:
        app = self.app
        yield Banner("Double-check everything, then press Install.")
        with Vertical(classes="card"):
            yield Static("Ready to install", classes="card-title")
            yield Static(
                f"[b]Game[/]     {escape(app.game.name)}\n"
                f"[b]AppID[/]    [#7dcfff]{app.game.appid}[/]\n"
                f"[b]Files[/]    {self.counts()}\n"
                f"[b]Folder[/]   {escape(str(app.folder))}\n"
                f"[b]Backend[/]  {app.backend.kind}" + ("   [yellow](dry run)[/]" if app.args.dry_run else "")
            )
        if app.backend.kind == "flatpak":
            with Vertical(classes="card", id="flatpak-card"):
                yield Static("[yellow]⚠ Flatpak sandbox[/]", classes="card-title")
                yield Static(
                    "Flatpak Protontricks can only read folders it has been granted.\n"
                    f"[cyan]flatpak override --user --filesystem={escape(shlex.quote(str(app.folder)))}:ro {FLATPAK_ID}[/]"
                )
                yield Button("Grant read access now", id="grant")
        with Horizontal(classes="row"):
            yield Switch(value=app.args.silent, id="silent")
            yield Label("Silent mode: add quiet flags for known installers (VC++, DirectX, .NET, OpenAL, any .msi)")
        with Vertical(classes="card", id="launch-card"):
            with Horizontal(classes="row"):
                yield Switch(value=app.args.launch_options is not None, id="set-launch")
                yield Label("Set Steam launch options for this game")
            yield Input(value=app.args.launch_options or DEFAULT_LAUNCH_OPTIONS, id="launch-value")
            with Horizontal(classes="row", id="replace-row"):
                yield Switch(value=app.args.replace_launch_options, id="launch-replace")
                yield Label("Replace existing options (off = merge, keeping what's already there)")
            yield Static("", id="launch-preview")
        yield VerticalScroll(Static(self.file_list(app.args.silent), id="files"), id="file-list")
        with Horizontal(classes="buttons"):
            yield Button("Back", id="back")
            yield Button("Install ▶", variant="success", id="install")
        yield Footer()

    def counts(self) -> str:
        n_msi = sum(p.suffix.lower() == ".msi" for p in self.app.exes)
        n_exe = len(self.app.exes) - n_msi
        return " + ".join(part for part in (f"{n_exe} .exe" if n_exe else "", f"{n_msi} .msi" if n_msi else "") if part)

    def file_list(self, silent: bool) -> str:
        lines = []
        for p in self.app.exes:
            rel = escape(str(p.relative_to(self.app.folder)))
            flags = silent_flags(p) if silent else []
            extra = f"  [dim]{escape(' '.join(flags))}[/]" if flags else ""
            lines.append(f"• {rel}{extra}")
        return "\n".join(lines)

    def on_mount(self) -> None:
        self.targets = find_launch_targets(self.app.game)
        self.refresh_launch()
        self.query_one("#install", Button).focus()

    # ── launch options ──

    def launch_plan(self) -> list[tuple[LaunchTarget, str]]:
        value = self.query_one("#launch-value", Input).value
        replace = self.query_one("#launch-replace", Switch).value
        return [(t, merge_launch_options(t.current, value, replace)) for t in self.targets]

    @on(Switch.Changed, "#set-launch")
    @on(Switch.Changed, "#launch-replace")
    @on(Input.Changed, "#launch-value")
    def refresh_launch(self) -> None:
        enabled = self.query_one("#set-launch", Switch).value
        for wid in ("#launch-value", "#replace-row", "#launch-preview"):
            self.query_one(wid).display = enabled
        if not enabled:
            return
        if not self.targets:
            self.query_one("#launch-preview", Static).update(
                "[yellow]⚠ This game isn't in your Steam config, so there's nothing to change.[/]"
            )
            return
        lines = []
        for target, new in self.launch_plan():
            where = "Steam game" if target.kind == "localconfig" else "non-Steam shortcut"
            lines.append(f"[b]{where}[/] [dim]({escape(str(target.path))})[/]")
            lines.append(f"  now  {escape(target.current) or '[dim](none)[/]'}")
            if new == target.current:
                lines.append("  [green]✔ already set; nothing to change[/]")
            else:
                lines.append(f"  [green]new[/]  {escape(new)}")
        lines.append("[dim]Steam must be closed while this is written; you'll be asked if it's running.[/]")
        self.query_one("#launch-preview", Static).update("\n".join(lines))

    @on(Switch.Changed, "#silent")
    def _silent(self, event: Switch.Changed) -> None:
        self.query_one("#files", Static).update(self.file_list(event.value))

    @on(Button.Pressed, "#grant")
    def _grant(self) -> None:
        cmd = ["flatpak", "override", "--user", f"--filesystem={self.app.folder}:ro", FLATPAK_ID]
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True)
        except OSError as exc:
            self.notify(str(exc), severity="error")
            return
        if proc.returncode == 0:
            self.notify("Access granted to the Flatpak sandbox ✔")
        else:
            self.notify(proc.stderr.strip() or "flatpak override failed", severity="error")

    @on(Button.Pressed, "#back")
    def _back(self) -> None:
        self.app.pop_screen()

    @on(Button.Pressed, "#install")
    def action_install(self) -> None:
        app = self.app
        app.silent = self.query_one("#silent", Switch).value
        app.launch_log = []
        if not self.query_one("#set-launch", Switch).value:
            self.start_install()
            return
        self.plan = [(t, new) for t, new in self.launch_plan() if new != t.current]
        if not self.targets:
            app.launch_log.append(("warn", "Launch options: game not found in Steam's config; skipped"))
        elif not self.plan:
            app.launch_log.append(("ok", "Launch options: already set; nothing changed"))
        elif app.args.dry_run:
            for target, new in self.plan:
                app.launch_log.append(("ok", f"Launch options (dry run, not written) → {new}  [{target.path}]"))
        elif steam_running():
            self.app.push_screen(SteamRunningScreen(), self._steam_choice)
            return
        else:
            self.apply_launch()
        self.start_install()

    def _steam_choice(self, choice: str | None) -> None:
        if choice == "closed":
            self.app.launch_log.append(("ok", "Closed Steam to save launch options; start it again when you're done"))
            self.apply_launch()
            self.start_install()
        elif choice == "skip":
            self.app.launch_log.append(("warn", "Launch options: skipped (Steam was running)"))
            self.start_install()

    def apply_launch(self) -> None:
        for target, new in self.plan:
            try:
                backup = set_launch_options(self.app.game, target, new)
            except (OSError, ValueError) as exc:
                self.app.launch_log.append(("fail", f"Launch options: couldn't write {target.path}: {exc}"))
                continue
            self.app.launch_log.append(("ok", f"Launch options set → {new}  [{target.path}; backup {backup.name}]"))

    def start_install(self) -> None:
        self.app.switch_screen(InstallScreen())


class SteamRunningScreen(ModalScreen):
    """Steam overwrites its config on exit, so offer to close it first."""

    BINDINGS = [Binding("escape", "cancel", "Cancel")]

    def compose(self) -> ComposeResult:
        with Vertical(id="steam-box"):
            yield Static("[b yellow]⚠ Steam is running[/]")
            yield Static(
                "Steam rewrites its config files when it exits, so launch options written now "
                "would be lost.\n\n[b]Close Steam[/] to apply them (this also closes any game running "
                "through Steam), or skip and just install."
            )
            yield Static("", id="steam-status")
            with Horizontal(classes="buttons"):
                yield Button("Cancel", id="steam-cancel")
                yield Button("Skip launch options", id="steam-skip")
                yield Button("Close Steam & apply", variant="warning", id="steam-close")

    def on_mount(self) -> None:
        self.query_one("#steam-skip", Button).focus()

    @on(Button.Pressed, "#steam-close")
    def close_clicked(self) -> None:
        for b in self.query(Button):
            b.disabled = True
        self.query_one("#steam-status", Static).update("[yellow]Waiting for Steam to exit…[/]")
        self.close_steam()

    @work(thread=True, exclusive=True)
    def close_steam(self) -> None:
        ok = shutdown_steam()
        self.app.call_from_thread(self.steam_exited, ok)

    def steam_exited(self, ok: bool) -> None:
        if ok:
            self.dismiss("closed")
            return
        for b in self.query(Button):
            b.disabled = False
        self.query_one("#steam-status", Static).update(
            "[red]✘ Steam didn't exit within 45 seconds.[/] Close it yourself, then press the button again."
        )

    @on(Button.Pressed, "#steam-skip")
    def skip_clicked(self) -> None:
        self.dismiss("skip")

    @on(Button.Pressed, "#steam-cancel")
    def action_cancel(self) -> None:
        self.dismiss(None)


STATUS_CELL = {
    "pending": Text("·  pending", style="dim"),
    "running": Text("▶  running", style="bold yellow"),
    "ok": Text("✔  ok", style="bold green"),
    "fail": Text("✘  failed", style="bold red"),
    "skipped": Text("⏭  skipped", style="#e0af68"),
}


class InstallScreen(Screen):
    BINDINGS = [
        Binding("s", "skip", "Skip current"),
        Binding("c", "cancel", "Cancel remaining"),
        Binding("q", "finish", "Quit", show=False),
    ]

    def __init__(self) -> None:
        super().__init__()
        self.proc: subprocess.Popen[str] | None = None
        self.skip_requested = threading.Event()
        self.cancel_requested = threading.Event()
        self.started = 0.0
        self.current_name = ""
        self.done = False

    def compose(self) -> ComposeResult:
        app = self.app
        tip = "Wine windows may pop up; click through them." if not app.silent else "Silent mode on; unknown installers may still show a window."
        yield Banner(tip)
        yield Static(
            f"[b]{escape(app.game.name)}[/] [#7dcfff]({app.game.appid})[/] · {len(app.exes)} installer(s) · "
            f"{escape(str(app.folder))}",
            id="current",
        )
        yield ProgressBar(total=len(app.exes), show_eta=False, id="progress")
        yield DataTable(id="results", cursor_type="none", zebra_stripes=True)
        yield RichLog(id="log", wrap=True, markup=True, highlight=False)
        with Horizontal(classes="buttons"):
            yield Button("Skip current", id="skip")
            yield Button("Cancel remaining", variant="error", id="cancel")
            yield Button("Done ✔", variant="success", id="done", classes="hidden")
        yield Footer()

    def on_mount(self) -> None:
        table = self.query_one("#results", DataTable)
        table.add_column("#", key="n")
        table.add_column("Installer", key="file")
        table.add_column("Status", key="status")
        table.add_column("Time", key="time")
        table.add_column("Note", key="note")
        for i, exe in enumerate(self.app.exes):
            table.add_row(str(i + 1), str(exe.relative_to(self.app.folder)), STATUS_CELL["pending"], "", "", key=str(i))
        for status, text in self.app.launch_log:
            style = {"ok": "green", "warn": "yellow", "fail": "red"}[status]
            self.log_line(f"[{style}]{escape(text)}[/]")
        self.set_interval(1, self.tick)
        self.run_all()

    # ── UI updates (main thread) ──

    def log_line(self, markup: str) -> None:
        self.query_one(RichLog).write(markup)

    def set_row(self, i: int, status: str, secs: float | None = None, note: str = "") -> None:
        table = self.query_one("#results", DataTable)
        table.update_cell(str(i), "status", STATUS_CELL[status], update_width=True)
        table.update_cell(str(i), "time", fmt_secs(secs) if secs is not None else "", update_width=True)
        table.update_cell(str(i), "note", Text(note, style="dim"), update_width=True)
        if status == "running":
            table.move_cursor(row=i)

    def set_current(self, i: int, name: str) -> None:
        self.current_name = name
        self.started = time.monotonic()
        self.tick()

    def tick(self) -> None:
        if self.done or not self.current_name:
            return
        n = len(self.app.exes)
        idx = self.query_one(ProgressBar).progress + 1
        elapsed = fmt_secs(time.monotonic() - self.started)
        self.query_one("#current", Static).update(
            f"[bold yellow]⏳ Installing {int(idx)}/{n}:[/] [b]{escape(self.current_name)}[/]  [dim]{elapsed}[/]"
        )

    def advance(self) -> None:
        self.query_one(ProgressBar).advance(1)

    def finished(self) -> None:
        self.done = True
        results = self.app.results
        ok = sum(r.status == "ok" for r in results)
        fail = sum(r.status == "fail" for r in results)
        skipped = sum(r.status == "skipped" for r in results)
        color = "green" if fail == 0 else "red"
        self.query_one("#current", Static).update(
            f"[bold {color}]{'✔ All done' if fail == 0 else '⚠ Finished with errors'}[/]  "
            f"[green]{ok} ok[/] · [red]{fail} failed[/] · [#e0af68]{skipped} skipped[/]   "
            "[dim]Press Enter or q to quit and print the summary.[/]"
        )
        self.refresh_bindings()
        self.query_one("#skip").display = False
        self.query_one("#cancel").display = False
        done = self.query_one("#done", Button)
        done.remove_class("hidden")
        done.focus()

    # ── actions ──

    @on(Button.Pressed, "#skip")
    def action_skip(self) -> None:
        if not self.done:
            self.skip_requested.set()
            self.kill_current()

    @on(Button.Pressed, "#cancel")
    def action_cancel(self) -> None:
        if not self.done:
            self.cancel_requested.set()
            self.kill_current()
            self.notify("Cancelling: remaining installers will be skipped.", severity="warning")

    @on(Button.Pressed, "#done")
    def action_finish(self) -> None:
        if self.done:
            self.app.exit()

    def check_action(self, action: str, parameters: tuple[object, ...]) -> bool | None:
        if action in ("skip", "cancel"):
            return not self.done
        if action == "finish":
            return self.done
        return True

    def kill_current(self) -> None:
        proc = self.proc
        if proc and proc.poll() is None:
            try:
                os.killpg(proc.pid, signal.SIGTERM)
            except (ProcessLookupError, PermissionError):
                pass

    # ── worker ──

    @work(thread=True, exclusive=True)
    def run_all(self) -> None:
        app = self.app
        ui = app.call_from_thread
        env = os.environ.copy()
        if not app.args.verbose:
            env.setdefault("WINEDEBUG", "-all")

        for i, exe in enumerate(app.exes):
            rel = str(exe.relative_to(app.folder))
            if self.cancel_requested.is_set():
                app.results.append(Result(exe, "skipped", None, "cancelled", 0))
                ui(self.set_row, i, "skipped", None, "cancelled")
                ui(self.advance)
                continue

            cmd = install_command(app.backend, app.game.appid, exe, app.silent)
            self.skip_requested.clear()
            ui(self.set_current, i, rel)
            ui(self.set_row, i, "running")
            ui(self.log_line, f"\n[bold #7aa2f7]━━ {escape(rel)}[/]\n[dim]$ {escape(shlex.join(cmd))}[/]")

            start = time.monotonic()
            code, tail = self.run_one(cmd, exe.parent, env)
            secs = time.monotonic() - start

            if self.skip_requested.is_set() or self.cancel_requested.is_set():
                result = Result(exe, "skipped", code, "stopped by you", secs)
            elif code is None:
                result = Result(exe, "fail", None, tail or "could not start", secs)
            elif code in OK_EXIT_CODES:
                result = Result(exe, "ok", code, OK_EXIT_CODES[code], secs)
            else:
                hint = failure_hint(tail, app.backend)
                result = Result(exe, "fail", code, f"exit {code}" + (f": {hint}" if hint else ""), secs)

            app.results.append(result)
            style = {"ok": "green", "fail": "red", "skipped": "#e0af68"}[result.status]
            ui(self.log_line, f"[{style}]→ {result.status.upper()}[/] {escape(result.note)} [dim]({fmt_secs(secs)})[/]")
            ui(self.set_row, i, result.status, secs, result.note)
            ui(self.advance)

        ui(self.finished)

    def run_one(self, cmd: list[str], cwd: Path, env: dict[str, str]) -> tuple[int | None, str]:
        """Run one installer, stream its output to the log. Returns (exit code, output tail)."""
        ui = self.app.call_from_thread
        if self.app.args.dry_run:
            time.sleep(0.8)
            ui(self.log_line, "[dim](dry run: not executed)[/]")
            return 0, ""
        try:
            self.proc = subprocess.Popen(
                cmd,
                cwd=cwd,
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                errors="replace",
                bufsize=1,
                start_new_session=True,  # lets "skip" kill the whole process group
            )
        except FileNotFoundError:
            return None, f"{cmd[0]} not found"
        except PermissionError:
            return None, f"permission denied running {cmd[0]}"

        tail: list[str] = []
        assert self.proc.stdout is not None
        for line in self.proc.stdout:
            line = line.rstrip()
            if not line:
                continue
            tail = (tail + [line])[-30:]
            ui(self.log_line, escape(line))
        code = self.proc.wait()
        self.proc = None
        return code, "\n".join(tail)


# ───────────────────────────── app ─────────────────────────────


class RedistApp(App):
    TITLE = "RedInstaller"
    CSS = CSS

    def __init__(self, backend: Backend | None, args: argparse.Namespace) -> None:
        super().__init__()
        self.backend = backend
        self.args = args
        self.game: Game | None = None
        self.folder: Path | None = None
        self.exes: list[Path] = []
        self.silent = args.silent
        self.results: list[Result] = []
        self.launch_log: list[tuple[str, str]] = []  # (ok|warn|fail, message)

    def on_mount(self) -> None:
        self.theme = "tokyo-night" if "tokyo-night" in self.available_themes else self.theme
        if self.backend is None:
            msg = install_instructions()
            if self.args.backend:
                msg = f"[b]The {self.args.backend} Protontricks wasn't found.[/]\n\n" + msg
            self.push_screen(ErrorScreen(msg))
        else:
            self.push_screen(GameScreen())

    def on_unmount(self) -> None:
        # Don't leave an installer running if the user quits mid-way.
        for screen in self.screen_stack:
            if isinstance(screen, InstallScreen):
                screen.cancel_requested.set()
                screen.kill_current()


def print_summary(app: RedistApp) -> None:
    console = Console()
    results = app.results
    if not results:
        return
    table = Table(box=None, pad_edge=False, show_header=True, header_style="bold")
    table.add_column("")
    table.add_column("Installer")
    table.add_column("Time", justify="right")
    table.add_column("Note", style="dim")
    icons = {"ok": "[green]✔[/]", "fail": "[red]✘[/]", "skipped": "[#e0af68]⏭[/]"}
    for r in results:
        table.add_row(icons[r.status], escape(str(r.exe.relative_to(app.folder))), fmt_secs(r.seconds), escape(r.note))
    ok = sum(r.status == "ok" for r in results)
    fail = sum(r.status == "fail" for r in results)
    skipped = sum(r.status == "skipped" for r in results)
    title = f"{escape(app.game.name)} ({app.game.appid})"
    subtitle = f"[green]{ok} succeeded[/] · [red]{fail} failed[/] · [#e0af68]{skipped} skipped[/]"
    console.print(Panel(table, title=title, subtitle=subtitle, border_style="red" if fail else "green", padding=(1, 2)))
    icons = {"ok": "[green]✔[/]", "warn": "[yellow]⚠[/]", "fail": "[red]✘[/]"}
    for status, text in app.launch_log:
        console.print(f"{icons[status]} {escape(text)}")
    if fail:
        console.print(
            "[dim]Tip: rerun with [b]--verbose[/] to keep Wine's debug output, or try a failed installer by hand:\n"
            f"  {escape(shlex.join([*app.backend.launch_cmd, '--appid', app.game.appid, '<file.exe>']))}[/]"
        )


def main() -> int:
    parser = argparse.ArgumentParser(description="Install Windows redistributables into a Proton prefix.")
    parser.add_argument("folder", nargs="?", help="folder containing the redistributable .exe/.msi files")
    parser.add_argument("--appid", help="preselect this Steam AppID")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--flatpak", dest="backend", action="store_const", const="flatpak", help="use Flatpak Protontricks")
    group.add_argument("--native", dest="backend", action="store_const", const="native", help="use native Protontricks")
    parser.add_argument("--recursive", action="store_true", help="include sub-folders by default")
    parser.add_argument("--silent", action="store_true", help="enable silent flags for known installers by default")
    parser.add_argument("--verbose", action="store_true", help="keep Wine debug output")
    parser.add_argument("--dry-run", action="store_true", help="show what would run without running it")
    parser.add_argument(
        "--launch-options", nargs="?", const=DEFAULT_LAUNCH_OPTIONS, metavar="OPTS",
        help="also set the game's Steam launch options (default: the WINEDLLOVERRIDES preset)",
    )
    parser.add_argument("--replace-launch-options", action="store_true", help="replace instead of merging")
    args = parser.parse_args()

    if not sys.stdin.isatty() or not sys.stdout.isatty():
        sys.stderr.write("redinstaller needs an interactive terminal.\n")
        return 2

    backend = detect_backend(args.backend)
    app = RedistApp(backend, args)
    app.run()
    if backend is None:
        Console().print(Panel(install_instructions(), border_style="red", title="Protontricks missing"))
        return 1
    print_summary(app)
    return 1 if any(r.status == "fail" for r in app.results) else 0


if __name__ == "__main__":
    sys.exit(main())
