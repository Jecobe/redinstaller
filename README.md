# Redinstaller

```
┏━┓┏━╸╺┳┓╻┏┓╻┏━┓╺┳╸┏━┓╻  ╻  ┏━╸┏━┓
┣┳┛┣╸  ┃┃┃┃┗┫┗━┓ ┃ ┣━┫┃  ┃  ┣╸ ┣┳┛
╹┗╸┗━╸╺┻┛╹╹ ╹┗━┛ ╹ ╹ ╹┗━╸┗━╸┗━╸╹┗╸
```

A terminal UI for Linux that installs a game's Windows redistributables
(the `.exe` / `.msi` files in `_CommonRedist`: VC++ runtimes, DirectX, .NET,
OpenAL, …) into its Steam Proton prefix with
[Protontricks](https://github.com/Matoking/protontricks). It works with Steam
games and non-Steam shortcuts.

Two versions are included:

| File | What it is | Needs |
|---|---|---|
| `redinstaller.py` | Full TUI built with [Textual](https://textual.textualize.io/) | Python 3.10+, Textual 2.1+, Protontricks |
| `redinstaller.sh` | Lighter bash version using `fzf` | bash 4.4+, Protontricks, optional `fzf` and `perl` |

Works on Arch, Debian, Ubuntu, Fedora and their derivatives, plus SteamOS and
other immutable distros through Flatpak Protontricks. Both versions detect
your distro and print the right install command if something is missing.

## Features

- **Game picker:** lists every Steam game and non-Steam shortcut that has a
  Proton prefix, with search by name or AppID.
- **Finds the redist folder for you.** It locates the game's install folder
  (from the Steam app manifest, or `shortcuts.vdf` for non-Steam games) and
  searches it for `_CommonRedist`, `Redist`, `Installers`, `DirectX`,
  `vcredist`, … For non-Steam games it also searches up to 4 parent folders,
  stopping below your home folder. You can also browse the game folder or
  type any path.
- **Installs `.exe` and `.msi` files** one at a time, with live progress, a
  per-file status table, and a summary at the end.
  - `.exe`: `protontricks-launch --appid <APPID> <file>`
  - `.msi`: `protontricks -c 'wine msiexec /i <file>' <APPID>`
- **Silent mode (optional):** adds quiet flags for known installers (VC++,
  DirectX, .NET, OpenAL, any `.msi`).
- **Treats "already installed" / "reboot requested" exit codes**
  (1638, 3010, 1641) as success.
- **Skip or cancel** while installers are running.
- **Steam launch options (optional):** writes launch options for the game,
  by default:
  ```
  WINEDLLOVERRIDES="OnlineFix64,SteamOverlay64,winmm,dnet,steam_api64=n,b" %command%
  ```
  It merges with what's already there (keeping your other options) unless
  you choose replace. See [Launch options](#launch-options).
- **Native or Flatpak Protontricks**, detected automatically. For Flatpak it
  can grant the sandbox read access to the redist folder.
- **`--dry-run`** shows every command without running anything.

## Install

```bash
git clone https://github.com/Jecobe/redinstaller.git
cd redinstaller
```

Then install the dependencies for your distro. `python3-textual` is only
needed for `redinstaller.py`; `fzf` and `perl` only for `redinstaller.sh`.

### Arch, Manjaro, EndeavourOS, CachyOS

```bash
sudo pacman -S protontricks python-textual fzf
```

### Fedora (and Nobara)

```bash
sudo dnf install protontricks python3-textual fzf perl-interpreter
```

### Ubuntu, Linux Mint, Pop!_OS

Protontricks is in the `multiverse` component:

```bash
sudo add-apt-repository multiverse
sudo apt install protontricks fzf
```

- **Ubuntu 25.04 and newer:** `sudo apt install python3-textual`.
- **Ubuntu 24.04 and older:** the packaged Textual is too old, so use a
  [virtual environment](#python-virtual-environment) for the Python version.

### Debian

Protontricks isn't packaged in Debian 13 (stable). Install it with `pipx`;
it also needs `winetricks`, which is in the `contrib` component (enable it in
your APT sources first):

```bash
sudo apt install pipx winetricks fzf
pipx install protontricks
pipx ensurepath              # puts protontricks on your PATH (log out and back in)
```

- **Debian 13 and newer:** `sudo apt install python3-textual`.
- **Debian 12:** use a [virtual environment](#python-virtual-environment).

If Steam came from Debian's or Ubuntu's `steam` package, it lives in
`~/.steam/debian-installation`. Redinstaller checks there automatically.

### SteamOS (Steam Deck), Bazzite, Fedora Silverblue

Use the Flatpak version of Protontricks:

```bash
flatpak install flathub com.github.Matoking.protontricks
```

For the Python version, use a
[virtual environment](#python-virtual-environment). The bash version works
without `fzf` (it falls back to numbered menus).

### Python virtual environment

Works on any distro when the packaged Textual is missing or too old:

```bash
sudo apt install python3-venv      # Debian/Ubuntu only
python3 -m venv ~/.venvs/redinstaller
~/.venvs/redinstaller/bin/pip install textual
~/.venvs/redinstaller/bin/python redinstaller.py
```

### Any distro

`pipx install protontricks` (needs `winetricks`) or the Flatpak above.

The game must have been launched once with Proton so its prefix exists.

## Usage

```bash
python3 redinstaller.py            # Textual TUI
./redinstaller.sh                  # bash + fzf version
```

1. Pick the game.
2. Pick a detected redist folder, browse, or paste a path.
3. Review the confirm screen (optionally turn on silent mode and launch
   options).
4. Install.

### Options (both versions)

| Option | Effect |
|---|---|
| `FOLDER` | Redist folder to use (skips the folder step's prompt) |
| `--appid ID` | Preselect a game |
| `--recursive` | Include installers in sub-folders |
| `--silent` | Use quiet flags for known installers |
| `--flatpak` / `--native` | Force a Protontricks flavour |
| `--verbose` | Keep Wine debug output (`WINEDEBUG` isn't silenced) |
| `--dry-run` | Print commands, run nothing |
| `--launch-options[=OPTS]` | Also set the game's Steam launch options |
| `--replace-launch-options` | Replace existing launch options instead of merging |

### Keys (Python version)

| Screen | Keys |
|---|---|
| Game list | type to filter · ↑/↓ · Enter select · Ctrl+R reload · Esc quit |
| Folder | Enter on a found folder · Ctrl+B browse · or type a path |
| Browser | Enter expand · `u` use folder · Backspace parent · `~` home |
| Confirm | `i` install · Esc back |
| Installing | `s` skip current · `c` cancel remaining |

## Launch options

- **Steam games:** written to `userdata/<account>/config/localconfig.vdf` for
  the logged-in account.
- **Non-Steam shortcuts:** written to the shortcut's entry in
  `userdata/*/config/shortcuts.vdf`.

Before writing, the original file is copied to
`<file>.redinstaller-<timestamp>.bak`. Only the `LaunchOptions` value is
changed; everything else in the file stays byte-for-byte the same.

Merging keeps your existing options and puts the new environment variables in
front, replacing any variable with the same name:

| Existing | Result |
|---|---|
| *(none)* | `WINEDLLOVERRIDES="…" %command%` |
| `gamemoderun %command%` | `WINEDLLOVERRIDES="…" gamemoderun %command%` |
| `-vulkan` | `WINEDLLOVERRIDES="…" %command% -vulkan` |
| `WINEDLLOVERRIDES="old" %command%` | `WINEDLLOVERRIDES="…" %command%` |

**Steam has to be closed.** Steam rewrites these files when it exits, so a
change made while it's running would be lost. If Steam is running, the tool
asks whether to close it (`steam -shutdown`, which also closes any game
running through Steam) or skip this step. It never closes Steam without
asking. To undo a change, restore the `.bak` file while Steam is closed.

The bash version uses `perl` to edit these files. Without `perl` it prints
the options for you to paste into Steam → Properties → Launch Options.

## Troubleshooting

- **"Protontricks was not found":** install it (see above), or pass
  `--flatpak` / `--native`.
- **Game missing from the list:** launch it once with Proton (Properties →
  Compatibility), then reload (Ctrl+R).
- **Flatpak: installer "not found":** the sandbox can't see the folder. Use
  the "Grant read access" button, or run
  `flatpak override --user --filesystem=/path/to/folder:ro com.github.Matoking.protontricks`.
- **An installer fails:** rerun with `--verbose`, or try it by hand:
  `protontricks-launch --appid <APPID> /path/to/file.exe`.
- **Steam can't be found:** set `STEAM_DIR=/path/to/Steam`.
