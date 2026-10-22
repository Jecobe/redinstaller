#!/usr/bin/env bash
# Redinstaller installer: installs the dependencies for your distro, then puts
# the `redinstaller` command on your PATH.
# https://github.com/Jecobe/redinstaller
#
#   curl -fsSL https://raw.githubusercontent.com/Jecobe/redinstaller/main/install.sh | bash
#
# Pass options after `bash -s --`, e.g. `... | bash -s -- --user`.
#
# Options:
#   --user        install to ~/.local/bin instead of /usr/local/bin (no root
#                 needed for the script itself; dependencies may still need sudo)
#   --no-deps     only install the script, skip dependencies
#   --flatpak     use Flatpak Protontricks even on a regular distro
#   --ref REF     install from this branch or tag (default: main)
#   -y, --yes     don't ask before installing packages
#   --uninstall   remove the redinstaller command
#   -h, --help    this help
#
# Supported: Arch (and Manjaro, EndeavourOS, CachyOS, …), Fedora (and Nobara),
# Ubuntu (and Mint, Pop!_OS, …), Debian, and SteamOS / Bazzite / Fedora Atomic
# through Flatpak. Other distros get Flatpak Protontricks if Flatpak is there.
#
# Copyright (C) 2026 Jecobe
# SPDX-License-Identifier: GPL-3.0-or-later

# Everything is inside main(), which runs on the last line, so a truncated
# download can't run half a script, and commands can't swallow the rest of the
# script from stdin when it's piped into bash.
main() {
set -euo pipefail

REPO=Jecobe/redinstaller
REF=main
FLATPAK_ID=com.github.Matoking.protontricks
USER_INSTALL=0 DEPS=1 FORCE_FLATPAK=0 YES=0 UNINSTALL=0

if [[ -t 1 ]]; then
  B=$'\e[1m' R=$'\e[0m' RED=$'\e[31m' GRN=$'\e[32m' YEL=$'\e[33m' CYN=$'\e[36m'
else
  B= R= RED= GRN= YEL= CYN=
fi
say()  { printf '%s==>%s %s%s%s\n' "$CYN$B" "$R" "$B" "$*" "$R"; }
ok()   { printf ' %s✓%s %s\n' "$GRN" "$R" "$*"; }
warn() { printf ' %s!%s %s\n' "$YEL" "$R" "$*" >&2; }
die()  { printf '%serror:%s %s\n' "$RED$B" "$R" "$*" >&2; exit 1; }
have() { command -v "$1" >/dev/null 2>&1; }

while (($#)); do
  case $1 in
    --user) USER_INSTALL=1 ;;
    --no-deps) DEPS=0 ;;
    --flatpak) FORCE_FLATPAK=1 ;;
    --ref) [[ $# -ge 2 ]] || die "--ref needs a value"; REF=$2; shift ;;
    --ref=*) REF=${1#*=} ;;
    -y|--yes) YES=1 ;;
    --uninstall) UNINSTALL=1 ;;
    -h|--help)
      if [[ -f ${BASH_SOURCE[0]:-} ]]; then
        sed -n '2,/^[^#]/{/^#/s/^# \{0,1\}//p}' "${BASH_SOURCE[0]}"
      else
        echo "See https://github.com/$REPO#install"
      fi
      exit 0 ;;
    *) die "unknown option: $1 (try --help)" ;;
  esac
  shift
done

# Keyboard input comes from the terminal, not stdin (stdin is this script when
# piped from curl). Without a terminal, package managers run non-interactively.
TTY=
if [[ -t 0 ]]; then TTY=/dev/stdin
elif { : </dev/tty; } 2>/dev/null; then TTY=/dev/tty
else YES=1
fi

ask() {  # ask "question" → 0 for yes (default yes)
  ((YES)) && return 0
  local a
  printf '%s?%s %s [Y/n] ' "$YEL$B" "$R" "$1"
  read -r a <"$TTY" || a=
  [[ -z $a || $a == [Yy]* ]]
}

if ((EUID == 0)); then SUDO=()
elif have sudo; then SUDO=(sudo)
elif have doas; then SUDO=(doas)
else SUDO=(missing-sudo)
fi
root() {  # run as root, with terminal input
  [[ ${SUDO[0]:-} == missing-sudo ]] && die "need root for: $* (install sudo, or run as root)"
  if [[ -n $TTY ]]; then "${SUDO[@]}" "$@" <"$TTY"; else "${SUDO[@]}" "$@" </dev/null; fi
}

# ── uninstall ────────────────────────────────────────────────────────────────
if ((UNINSTALL)); then
  removed=0
  for f in "$HOME/.local/bin/redinstaller" /usr/local/bin/redinstaller; do
    [[ -e $f ]] || continue
    if [[ -w ${f%/*} ]]; then rm -f "$f"; else root rm -f "$f"; fi
    ok "removed $f"; removed=1
  done
  [[ -e /usr/bin/redinstaller ]] &&
    warn "/usr/bin/redinstaller belongs to a package; remove it with your package manager (e.g. sudo pacman -R redinstaller)"
  ((removed)) || warn "redinstaller isn't installed in /usr/local/bin or ~/.local/bin"
  echo "Dependencies (protontricks, fzf, …) were left installed."
  exit 0
fi

# ── detect the distro ────────────────────────────────────────────────────────
ID= ID_LIKE= PRETTY_NAME=
if [[ -r /etc/os-release ]]; then
  eval "$(grep -E '^(ID|ID_LIKE|PRETTY_NAME)=' /etc/os-release)"
fi
DISTRO=other
if [[ $ID == steamos || -e /run/ostree-booted ]]; then
  DISTRO=immutable
else
  for f in ubuntu debian fedora arch; do   # ubuntu first: Mint & co say "ubuntu debian"
    [[ " $ID $ID_LIKE " == *" $f "* ]] && { DISTRO=$f; break; }
  done
fi
((FORCE_FLATPAK)) && DISTRO=flatpak-forced
# Read-only systems can't take packages or /usr/local writes reliably.
[[ $DISTRO == immutable ]] && USER_INSTALL=1

say "Installing Redinstaller on ${PRETTY_NAME:-$ID} ($DISTRO)"

# ── dependencies ─────────────────────────────────────────────────────────────
has_protontricks() {
  have protontricks-launch && return 0
  have flatpak && flatpak info "$FLATPAK_ID" >/dev/null 2>&1
}

flatpak_protontricks() {
  have flatpak || die "Flatpak isn't installed. Install flatpak from your package manager, then run this again."
  if ! flatpak remotes --user 2>/dev/null | grep -q '^flathub' &&
     ! flatpak remotes --system 2>/dev/null | grep -q '^flathub'; then
    say "Adding the Flathub remote (for your user)"
    flatpak remote-add --user --if-not-exists flathub https://dl.flathub.org/repo/flathub.flatpakrepo
  fi
  say "Installing Flatpak Protontricks"
  local yes=(); ((YES)) && yes=(-y)
  if [[ -n $TTY ]]; then flatpak install --user "${yes[@]}" flathub "$FLATPAK_ID" <"$TTY"
  else flatpak install --user -y --noninteractive flathub "$FLATPAK_ID"; fi
}

install_deps() {
  local y=()
  case $DISTRO in
    arch)
      ((YES)) && y=(--noconfirm)
      ask "Update the system and install protontricks, fzf and perl with pacman (-Syu)?" || return 0
      root pacman -Syu --needed "${y[@]}" protontricks fzf perl ;;
    fedora)
      ((YES)) && y=(-y)
      ask "Install protontricks, fzf and perl with dnf?" || return 0
      root dnf install "${y[@]}" protontricks fzf perl-interpreter ;;
    ubuntu)
      ((YES)) && y=(-y)
      ask "Install protontricks, fzf and perl with apt (enables the multiverse component if needed)?" || return 0
      root apt-get update
      if [[ -z $(apt-cache policy protontricks 2>/dev/null | awk '/Candidate:/ {print $2}' | grep -v none) ]]; then
        have add-apt-repository || root apt-get install -y software-properties-common
        root add-apt-repository -y multiverse
        root apt-get update
      fi
      root apt-get install "${y[@]}" protontricks fzf perl ;;
    debian)
      # Protontricks isn't packaged in Debian stable: use pipx. winetricks is in contrib.
      ((YES)) && y=(-y)
      ask "Install pipx, winetricks, fzf and perl with apt, then protontricks with pipx?" || return 0
      root apt-get update
      local pkgs=(pipx fzf perl)
      if [[ -n $(apt-cache policy winetricks 2>/dev/null | awk '/Candidate:/ {print $2}' | grep -v none) ]]; then
        pkgs+=(winetricks)
      else
        warn "winetricks isn't available: enable the \"contrib\" component in your APT sources, then run: sudo apt install winetricks"
      fi
      root apt-get install "${y[@]}" "${pkgs[@]}"
      say "Installing protontricks with pipx (for ${USER:-$(id -un)})"
      pipx install protontricks || pipx upgrade protontricks
      pipx ensurepath >/dev/null 2>&1 || true
      export PATH="$HOME/.local/bin:$PATH" ;;
    immutable|flatpak-forced)
      ask "Install Protontricks from Flathub (Flatpak)?" || return 0
      flatpak_protontricks ;;
    *)
      warn "Unrecognised distro (ID=$ID)."
      if have flatpak; then
        ask "Install Protontricks from Flathub (Flatpak)?" || return 0
        flatpak_protontricks
      else
        warn "Install protontricks (and optionally fzf and perl) yourself: https://github.com/Matoking/protontricks#installation"
      fi ;;
  esac
}

if ((DEPS)); then
  if has_protontricks && have fzf && have perl; then
    ok "protontricks, fzf and perl are already installed"
  else
    install_deps
  fi
  # Flatpak users don't need fzf/perl from the host package manager (optional).
  have fzf || warn "fzf not installed: you'll get numbered menus instead of search (optional)"
  have perl || warn "perl not installed: Redinstaller can't set Steam launch options (optional)"
  if has_protontricks; then ok "Protontricks found"
  else warn "Protontricks still isn't installed; Redinstaller needs it to run"
  fi
fi

# ── the script itself ────────────────────────────────────────────────────────
if ((USER_INSTALL)); then DEST=$HOME/.local/bin
else DEST=/usr/local/bin
fi

TMP=$(mktemp)
trap 'rm -f "$TMP"' EXIT
here=
[[ -f ${BASH_SOURCE[0]:-} ]] && here=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
URL=https://raw.githubusercontent.com/$REPO/$REF/redinstaller
if [[ -n $here && -f $here/redinstaller && -f $here/install.sh ]]; then
  say "Using redinstaller from $here"
  cp "$here/redinstaller" "$TMP"
else
  say "Downloading redinstaller ($REF)"
  if have curl; then curl -fsSL "$URL" -o "$TMP" || die "download failed: $URL"
  elif have wget; then wget -qO "$TMP" "$URL" || die "download failed: $URL"
  elif have python3; then
    python3 -c 'import sys, urllib.request; urllib.request.urlretrieve(sys.argv[1], sys.argv[2])' "$URL" "$TMP" ||
      die "download failed: $URL"
  else die "need curl, wget or python3 to download $URL"
  fi
fi
head -n1 "$TMP" | grep -q '^#!.*bash' || die "the download doesn't look like the redinstaller script"
version=$(bash "$TMP" --version 2>/dev/null) || die "the downloaded script doesn't run (bash $(bash -c 'echo $BASH_VERSION'))"

if [[ -w $DEST ]] || { [[ ! -e $DEST ]] && ((USER_INSTALL)); }; then
  install -Dm755 "$TMP" "$DEST/redinstaller"
else
  root install -Dm755 "$TMP" "$DEST/redinstaller"
fi
ok "installed $version to $DEST/redinstaller"

if [[ -e /usr/bin/redinstaller && $DEST != /usr/bin ]]; then
  warn "/usr/bin/redinstaller (from a package) also exists; whichever comes first on PATH wins"
fi
case ":$PATH:" in
  *":$DEST:"*) ;;
  *) warn "$DEST isn't on your PATH. Add this to your shell config (e.g. ~/.bashrc), then open a new terminal:"
     printf '     export PATH="%s:$PATH"\n' "$DEST" ;;
esac
if [[ $DISTRO == debian ]]; then
  echo "   If 'protontricks' isn't found yet, log out and back in (pipx added ~/.local/bin to PATH)."
fi

printf '\n%sDone.%s Run %sredinstaller%s to get started (the game needs to have been launched once with Proton).\n' \
  "$GRN$B" "$R" "$B" "$R"
}

main "$@"
