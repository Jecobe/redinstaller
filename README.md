# Redinstaller

```
┏━┓┏━╸╺┳┓╻┏┓╻┏━┓╺┳╸┏━┓╻  ╻  ┏━╸┏━┓
┣┳┛┣╸  ┃┃┃┃┗┫┗━┓ ┃ ┣━┫┃  ┃  ┣╸ ┣┳┛
╹┗╸┗━╸╺┻┛╹╹ ╹┗━┛ ╹ ╹ ╹┗━╸┗━╸┗━╸╹┗╸
```

A terminal tool for Linux that installs a game's Windows redistributables
(the `.exe` / `.msi` files in `_CommonRedist`: VC++ runtimes, DirectX, .NET,
OpenAL, …) into its Steam Proton prefix with
[Protontricks](https://github.com/Matoking/protontricks). It works with Steam
games and non-Steam shortcuts.

It's a single bash script. It works on Arch, Debian, Ubuntu, Fedora and their
derivatives, plus SteamOS and other immutable distros through Flatpak
Protontricks. If something is missing, it detects your distro and prints the
right install command.

## Features

- **Game picker:** lists every Steam game and non-Steam shortcut that has a
  Proton prefix, as `Name  |  AppID`, searchable with
  [fzf](https://github.com/junegunn/fzf) (or a numbered menu without it).
- **Finds the redist folder for you.** It locates the game's install folder
  (from the Steam app manifest, or `shortcuts.vdf` for non-Steam games) and
  searches it for `_CommonRedist`, `Redist`, `Installers`, `DirectX`,
  `vcredist`, … For non-Steam games it also searches up to 4 parent folders,
  stopping below your home folder. You can also browse the game folder
  (with a preview of the installers in each folder) or type any path.
- **Installs `.exe` and `.msi` files** one at a time, with a progress bar, the
  installer's output, and a summary at the end.
  - `.exe`: `protontricks-launch --appid <APPID> <file>`
  - `.msi`: `protontricks -c 'wine msiexec /i <file>' <APPID>`
- **Silent mode (optional):** adds quiet flags for known installers (VC++,
  DirectX, .NET, OpenAL, any `.msi`).
- **Treats "already installed" / "reboot requested" exit codes**
  (1638, 3010, 1641) as success.
- **Ctrl+C skips** the running installer and asks whether to continue.
- **Steam launch options (optional):** writes launch options for the game,
  by default:
  ```
  WINEDLLOVERRIDES="OnlineFix64,SteamOverlay64,winmm,dnet,steam_api64=n,b" %command%
  ```
  It merges with what's already there (keeping your other options) unless
  you choose replace. See [Launch options](#launch-options).
- **Native or Flatpak Protontricks**, detected automatically.
- **`--dry-run`** shows every command without running anything.

## Install

### Arch Linux (AUR)

```bash
yay -S redinstaller          # or paru -S redinstaller, or any AUR helper
```

This installs the `redinstaller` command along with `protontricks`. `fzf`
(searchable menus) and `perl` (launch options) are optional dependencies:

```bash
sudo pacman -S fzf perl
```

### Other distros

Install the dependencies (below), then put the script on your `PATH`:

```bash
git clone https://github.com/Jecobe/redinstaller.git
sudo install -Dm755 redinstaller/redinstaller /usr/local/bin/redinstaller
```

Or, without root: `install -Dm755 redinstaller/redinstaller ~/.local/bin/redinstaller`
(make sure `~/.local/bin` is on your `PATH`).

#### Fedora (and Nobara)

```bash
sudo dnf install protontricks fzf perl-interpreter
```

#### Ubuntu, Linux Mint, Pop!_OS

Protontricks is in the `multiverse` component:

```bash
sudo add-apt-repository multiverse
sudo apt install protontricks fzf
```

#### Debian

Protontricks isn't packaged in Debian 13 (stable). Install it with `pipx`;
it also needs `winetricks`, which is in the `contrib` component (enable it in
your APT sources first):

```bash
sudo apt install pipx winetricks fzf
pipx install protontricks
pipx ensurepath              # puts protontricks on your PATH (log out and back in)
```

If Steam came from Debian's or Ubuntu's `steam` package, it lives in
`~/.steam/debian-installation`. Redinstaller checks there automatically.

#### SteamOS (Steam Deck), Bazzite, Fedora Silverblue

Use the Flatpak version of Protontricks, and install the script to
`~/.local/bin` (see above):

```bash
flatpak install flathub com.github.Matoking.protontricks
```

`fzf` is optional; without it you get numbered menus.

The game must have been launched once with Proton so its prefix exists.

## Usage

```bash
redinstaller
```

1. Pick the game.
2. Pick a detected redist folder, browse, or type a path.
3. Review what will be installed (optionally set launch options).
4. Install.

### Options

| Option | Effect |
|---|---|
| `FOLDER` | Redist folder to use (skips the folder step) |
| `--appid ID` | Preselect a game |
| `--recursive` | Include installers in sub-folders |
| `--silent` | Use quiet flags for known installers |
| `--flatpak` / `--native` | Force a Protontricks flavour |
| `--verbose` | Keep Wine debug output (`WINEDEBUG` isn't silenced) |
| `--dry-run` | Print commands, run nothing |
| `--launch-options[=OPTS]` | Also set the game's Steam launch options |
| `--replace-launch-options` | Replace existing launch options instead of merging |
| `--version` | Print the version |
| `-h`, `--help` | Show help |

## Launch options

- **Steam games:** written to `userdata/<account>/config/localconfig.vdf` for
  the logged-in account.
- **Non-Steam shortcuts:** written to the shortcut's entry in
  `userdata/*/config/shortcuts.vdf`.

Before writing, the original file is copied to
`<file>.redinstaller-<timestamp>.bak`. Only the `LaunchOptions` value is
changed; everything else in the file stays byte-for-byte the same. This step
needs `perl`; without it, Redinstaller prints the options for you to paste
into Steam → Properties → Launch Options.

Merging keeps your existing options and puts the new environment variables in
front, replacing any variable with the same name:

| Existing | Result |
|---|---|
| *(none)* | `WINEDLLOVERRIDES="…" %command%` |
| `gamemoderun %command%` | `WINEDLLOVERRIDES="…" gamemoderun %command%` |
| `-vulkan` | `WINEDLLOVERRIDES="…" %command% -vulkan` |
| `WINEDLLOVERRIDES="old" %command%` | `WINEDLLOVERRIDES="…" %command%` |

**Steam has to be closed.** Steam rewrites these files when it exits, so a
change made while it's running would be lost. If Steam is running, Redinstaller
asks whether to close it (`steam -shutdown`, which also closes any game
running through Steam) or skip this step. It never closes Steam without
asking. To undo a change, restore the `.bak` file while Steam is closed.

## Troubleshooting

- **"Protontricks was not found":** install it (see above), or pass
  `--flatpak` / `--native`.
- **Game missing from the list:** launch it once with Proton (Properties →
  Compatibility), then run Redinstaller again.
- **Flatpak: installer "not found":** the sandbox can't see the folder. Run
  the `flatpak override` command Redinstaller prints, or:
  `flatpak override --user --filesystem=/path/to/folder:ro com.github.Matoking.protontricks`.
- **An installer fails:** rerun with `--verbose`, or try it by hand:
  `protontricks-launch --appid <APPID> /path/to/file.exe`.
- **Steam can't be found:** set `STEAM_DIR=/path/to/Steam`.

## License

[GPL-3.0-or-later](LICENSE)
