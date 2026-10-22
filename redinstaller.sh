#!/usr/bin/env bash
# redinstaller.sh: install a folder of Windows redistributable .exe/.msi files into
# a Steam Proton prefix with protontricks-launch. Dependency-light version of
# redinstaller.py.
#
# Needs:  bash 4+, protontricks (native or Flatpak), fzf (optional; falls back
#         to a numbered menu). Arch: sudo pacman -S protontricks fzf
#
# Finds the game's install folder (Steam appmanifest, or shortcuts.vdf for
# non-Steam games) and searches it for _CommonRedist-style folders; you can
# pick one, browse the game folder, or type a custom path.
#
# Usage:  ./redinstaller.sh [options] [FOLDER]
#   --appid ID     skip the game picker
#   --recursive    include installers in sub-folders
#   --silent       add quiet flags for known installers (VC++, DirectX, .NET, OpenAL)
#   --flatpak      force Flatpak Protontricks      --native   force native
#   --verbose      keep Wine debug output          --dry-run  print commands only
#   --launch-options[=OPTS]   also set the game's Steam launch options (default:
#                  WINEDLLOVERRIDES="OnlineFix64,SteamOverlay64,winmm,dnet,steam_api64=n,b" %command%)
#   --replace-launch-options  replace existing launch options instead of merging
#
# Launch options are written to localconfig.vdf / shortcuts.vdf (needs perl),
# after a .bak backup. Steam must be closed first; the script offers to close it.
#
# Ctrl+C while an installer runs skips that installer and asks whether to go on.

set -uo pipefail
shopt -s extglob

FLATPAK_ID=com.github.Matoking.protontricks

# ── colours ──────────────────────────────────────────────────────────────────
if [[ -t 1 ]]; then
  B=$'\e[1m' D=$'\e[2m' R=$'\e[0m' RED=$'\e[31m' GRN=$'\e[32m' YEL=$'\e[33m'
  BLU=$'\e[38;5;111m' MAG=$'\e[38;5;141m' CYN=$'\e[36m'
else
  B= D= R= RED= GRN= YEL= BLU= MAG= CYN=
fi
die()  { printf '%s✘ %s%s\n' "$RED$B" "$*" "$R" >&2; exit 1; }
info() { printf '%s›%s %s\n' "$BLU" "$R" "$*"; }
ok()   { printf '%s✔%s %s\n' "$GRN" "$R" "$*"; }
warn() { printf '%s⚠ %s%s\n' "$YEL" "$*" "$R"; }

banner() {
  printf '%s%s\n' "$BLU$B" "┏━┓┏━╸╺┳┓╻┏┓╻┏━┓╺┳╸┏━┓╻  ╻  ┏━╸┏━┓"
  printf '%s%s\n' "$MAG$B" "┣┳┛┣╸  ┃┃┃┃┗┫┗━┓ ┃ ┣━┫┃  ┃  ┣╸ ┣┳┛"
  printf '%s%s%s\n' "$MAG$B" "╹┗╸┗━╸╺┻┛╹╹ ╹┗━┛ ╹ ╹ ╹┗━╸┗━╸┗━╸╹┗╸" "$R"
  printf '%sWindows redistributables → Steam Proton prefixes, via Protontricks%s\n' "$CYN" "$R"
  printf '%s💡 Pick a game, paste the _CommonRedist folder, confirm. Ctrl+C skips an installer.%s\n\n' "$D" "$R"
}

# ── args ─────────────────────────────────────────────────────────────────────
APPID= FOLDER= RECURSIVE=0 SILENT=0 FORCE= VERBOSE=0 DRYRUN=0
DEFAULT_LAUNCH='WINEDLLOVERRIDES="OnlineFix64,SteamOverlay64,winmm,dnet,steam_api64=n,b" %command%'
LAUNCH= LAUNCH_VALUE=$DEFAULT_LAUNCH LAUNCH_REPLACE=0
while (($#)); do
  case $1 in
    --appid) APPID=${2:?--appid needs a value}; shift ;;
    --appid=*) APPID=${1#*=} ;;
    --recursive|-r) RECURSIVE=1 ;;
    --silent|-s) SILENT=1 ;;
    --flatpak) FORCE=flatpak ;;
    --native) FORCE=native ;;
    --verbose|-v) VERBOSE=1 ;;
    --dry-run|-n) DRYRUN=1 ;;
    --launch-options) LAUNCH=1 ;;
    --launch-options=*) LAUNCH=1 LAUNCH_VALUE=${1#*=} ;;
    --replace-launch-options) LAUNCH=1 LAUNCH_REPLACE=1 ;;
    -h|--help) sed -n '2,27s/^# \{0,1\}//p' "$0"; exit 0 ;;
    -*) die "Unknown option: $1 (see --help)" ;;
    *) FOLDER=$1 ;;
  esac
  shift
done

banner

# ── backend ──────────────────────────────────────────────────────────────────
install_help() {
  cat >&2 <<EOF
${B}Protontricks was not found.${R} Install it with one of:
  Arch / Manjaro   ${CYN}sudo pacman -S protontricks${R}
  Debian / Ubuntu  ${CYN}sudo apt install protontricks${R}
  Fedora           ${CYN}sudo dnf install protontricks${R}
  Any distro       ${CYN}pipx install protontricks${R}
  Flatpak          ${CYN}flatpak install flathub $FLATPAK_ID${R}
EOF
  exit 1
}
have_native()  { command -v protontricks >/dev/null && command -v protontricks-launch >/dev/null; }
have_flatpak() { command -v flatpak >/dev/null && flatpak info "$FLATPAK_ID" >/dev/null 2>&1; }

case $FORCE in
  native)  have_native  || install_help; KIND=native ;;
  flatpak) have_flatpak || install_help; KIND=flatpak ;;
  *) if have_native; then KIND=native; elif have_flatpak; then KIND=flatpak; else install_help; fi ;;
esac
if [[ $KIND == native ]]; then
  PT_LIST=(protontricks); PT_LAUNCH=(protontricks-launch)
else
  PT_LIST=(flatpak run "$FLATPAK_ID"); PT_LAUNCH=(flatpak run --command=protontricks-launch "$FLATPAK_ID")
fi
info "Using ${B}$KIND${R} Protontricks"

# ── games: "APPID<TAB>NAME" lines ────────────────────────────────────────────
steam_roots() {
  local root
  for root in "${STEAM_DIR:-}" ~/.local/share/Steam ~/.steam/steam ~/.steam/root \
              ~/.var/app/com.valvesoftware.Steam/.local/share/Steam ~/snap/steam/common/.local/share/Steam; do
    [[ -n $root && -d $root/steamapps ]] && realpath "$root"
  done | sort -u
}

steam_libs() {
  local root lib
  while IFS= read -r root; do
    echo "$root"
    while IFS= read -r lib; do
      [[ -d $lib/steamapps ]] && realpath "$lib"
    done < <(sed -n 's/^[[:space:]]*"path"[[:space:]]*"\(.*\)"/\1/p' "$root/steamapps/libraryfolders.vdf" 2>/dev/null)
  done < <(steam_roots) | sort -u
}

scan_steam() {
  local lib acf id name
  while IFS= read -r lib; do
    for acf in "$lib"/steamapps/appmanifest_*.acf; do
      [[ -f $acf ]] || continue
      id=${acf##*_}; id=${id%.acf}
      [[ -d $lib/steamapps/compatdata/$id/pfx ]] || continue
      name=$(sed -n 's/^[[:space:]]*"name"[[:space:]]*"\(.*\)"/\1/p' "$acf" | head -n1)
      printf '%s\t%s\n' "$id" "${name:-App $id}"
    done
  done < <(steam_libs)
}

list_games() {
  local out
  out=$("${PT_LIST[@]}" -l 2>/dev/null </dev/null | sed -n 's/^\(.*\) (\([0-9]\{1,\}\))[[:space:]]*$/\2\t\1/p')
  [[ -n $out ]] || out=$(scan_steam)
  [[ -n $out ]] && sort -t $'\t' -k2,2f -u <<<"$out"
}

info "Looking for games with a Proton prefix…"
GAMES=$(list_games) || true
[[ -n $GAMES ]] || die "No games with a Proton prefix found. Launch the game once with Proton enabled, then retry."
ok "$(wc -l <<<"$GAMES") games found"

if [[ -n $APPID ]]; then
  PICK=$(awk -F'\t' -v id="$APPID" '$1 == id' <<<"$GAMES" | head -n1)
  [[ -n $PICK ]] || die "AppID $APPID has no Proton prefix."
elif command -v fzf >/dev/null; then
  PICK=$(awk -F'\t' -v OFS='\t' '{ print $1, $2, $2 "  |  " $1 }' <<<"$GAMES" |
         fzf --delimiter=$'\t' --with-nth=3 --height=60% --reverse --border=rounded \
             --prompt='🔎 Game › ' --header='Type to filter · Enter to select · Esc to quit' \
             --info=inline) || die "No game selected."
else
  warn "fzf not found; using a numbered menu (sudo pacman -S fzf for search)."
  mapfile -t rows <<<"$GAMES"
  labels=(); for r in "${rows[@]}"; do labels+=("${r#*$'\t'}  ${D}|${R}  ${r%%$'\t'*}"); done
  PS3="${B}Game number › ${R}"
  select _ in "${labels[@]}"; do
    [[ -n ${REPLY//[^0-9]/} && $REPLY -ge 1 && $REPLY -le ${#rows[@]} ]] && { PICK=${rows[REPLY-1]}; break; }
    warn "Pick a number between 1 and ${#rows[@]}."
  done
  [[ -n ${PICK:-} ]] || die "No game selected."
fi
IFS=$'\t' read -r APPID NAME _ <<<"$PICK"
ok "Game: ${B}$NAME${R} ${CYN}($APPID)${R}"
echo

# ── where are the redistributables? ──────────────────────────────────────────
# Prints "DIR<TAB>SOURCE" for the game's install folder.
game_dir() {
  local appid=$1 name=${2#Non-Steam shortcut: } lib dir root f d
  while IFS= read -r lib; do
    dir=$(sed -n 's/^[[:space:]]*"installdir"[[:space:]]*"\(.*\)"/\1/p' "$lib/steamapps/appmanifest_$appid.acf" 2>/dev/null | head -n1)
    if [[ -n $dir && -d $lib/steamapps/common/$dir ]]; then
      printf '%s\tSteam install folder\n' "$lib/steamapps/common/$dir"; return
    fi
  done < <(steam_libs)
  # Non-Steam shortcut: shortcuts.vdf is binary; split on the type bytes and
  # read the StartDir / Exe that follow the matching AppName.
  while IFS= read -r root; do
    for f in "$root"/userdata/*/config/shortcuts.vdf; do
      [[ -f $f ]] || continue
      while IFS= read -r d; do
        if [[ $d == /* && -d $d ]]; then
          printf '%s\tnon-Steam shortcut start folder\n' "${d%/}"; return
        fi
      done < <(LC_ALL=C tr '\000\001\002\010' '\n\n\n\n' <"$f" | LC_ALL=C awk -v want="$name" '
        tolower($0) == "appname"  { getline; cur = $0; next }
        tolower($0) == "exe"      { getline; exe[cur] = $0; next }
        tolower($0) == "startdir" { getline; sd[cur] = $0; next }
        END { d = sd[want]; gsub(/"/, "", d); e = exe[want]; gsub(/"/, "", e); sub(/\/[^\/]*$/, "", e); print d; print e }')
    done
  done < <(steam_roots)
}

# Prints "COUNT<TAB>DIR" for every _CommonRedist-like folder, best match first.
find_redist_dirs() {
  local d n
  while IFS= read -r -d '' d; do
    n=$(find "$d" -type f \( -iname '*.exe' -o -iname '*.msi' \) 2>/dev/null | wc -l)
    ((n)) && printf '%s\t%s\n' "$n" "$d"
  done < <(timeout 5 find "$1" -mindepth 1 -maxdepth 5 \( -name '.*' -prune \) -o -type d \
             \( -iname _commonredist -o -iname commonredist -o -iname _redist -o -iname redist \
                -o -iname redists -o -iname 'redistributable*' -o -iname _installer -o -iname __installer \
                -o -iname installers -o -iname directx -o -iname vcredist \) -print0 -prune 2>/dev/null) |
    awk -F'\t' -v OFS='\t' '{ k = (tolower($2) ~ /commonredist$/) ? 0 : 1; p = $2; print k, gsub("/", "/", p), $0 }' |
    sort -t $'\t' -k1,1n -k2,2n | cut -f3-
}

# pick_menu PROMPT "KEY<TAB>LABEL lines" → prints the chosen KEY
pick_menu() {
  local prompt=$1 lines=$2 r
  if command -v fzf >/dev/null; then
    fzf --delimiter=$'\t' --with-nth=2.. --no-sort --height=40% --reverse --border=rounded \
        --prompt="$prompt › " --info=hidden <<<"$lines" | cut -f1
  else
    local -a rows=() labels=()
    mapfile -t rows <<<"$lines"
    for r in "${rows[@]}"; do labels+=("${r#*$'\t'}"); done
    PS3="${B}${prompt} › ${R}"
    select _ in "${labels[@]}"; do
      if [[ $REPLY =~ ^[0-9]+$ ]] && ((REPLY >= 1 && REPLY <= ${#rows[@]})); then
        printf '%s\n' "${rows[REPLY-1]%%$'\t'*}"; return
      fi
    done
  fi
}

# browse ROOT → prints the chosen folder (fzf over sub-folders, or readline with Tab completion)
browse() {
  local root=$1 rel
  if command -v fzf >/dev/null; then
    rel=$({ echo .; find "$root" -mindepth 1 -maxdepth 6 \( -name '.*' -prune \) -o -type d -printf '%P\n' 2>/dev/null | sort -f; } |
      fzf --height=70% --reverse --border=rounded --prompt='📂 Folder › ' \
          --header="Inside: $root   (Enter picks · Esc cancels)" \
          --preview="cd $(printf %q "$root") && find {} -maxdepth 3 -type f \( -iname '*.exe' -o -iname '*.msi' \) 2>/dev/null | head -50" \
          --preview-label=' installers ') || return 1
    printf '%s' "$root/${rel#.}"
  else
    read -e -r -i "$root/" -p "${B}📂 Folder (Tab completes):${R} " rel && printf '%s' "$rel"
  fi
}

if [[ -z $FOLDER ]]; then
  GAMEDIR= GAMEDIR_SRC= ROOT= UP=0
  IFS=$'\t' read -r GAMEDIR GAMEDIR_SRC < <(game_dir "$APPID" "$NAME") || true
  FOUND=()
  if [[ -n $GAMEDIR ]]; then
    ok "Install folder: $GAMEDIR ${D}($GAMEDIR_SRC)${R}"
    info "Searching it for _CommonRedist-style folders…"
    ROOT=$GAMEDIR UP=0
    mapfile -t FOUND < <(find_redist_dirs "$ROOT")
    # Non-Steam shortcuts often start in a sub-folder (bin/, Binaries/…), so
    # climb up to MAX_UP levels, stopping at the first level with a hit. Never
    # search $HOME itself or above it (other games' installers live there), nor /.
    if ((${#FOUND[@]} == 0)) && [[ $GAMEDIR_SRC == non-Steam* ]]; then
      MAX_UP=4
      while ((${#FOUND[@]} == 0 && UP < MAX_UP)); do
        parent=$(dirname -- "$ROOT")
        [[ $parent == "$ROOT" || $parent == / ]] && break
        [[ $GAMEDIR == "$HOME"/* && $parent != "$HOME"/* ]] && break
        ROOT=$parent UP=$((UP + 1))
        info "Nothing yet; searching ${UP} level(s) up: $ROOT"
        mapfile -t FOUND < <(find_redist_dirs "$ROOT")
      done
    fi
    if ((${#FOUND[@]})); then
      ok "Found ${#FOUND[@]} candidate folder(s)$( ((UP)) && echo " ${UP} level(s) above the game folder")"
    elif [[ $GAMEDIR_SRC == non-Steam* ]]; then
      printf '%s✘ No _CommonRedist-style folder in the game folder or %d level(s) above it (searched up to %s).%s\n' "$RED$B" "$UP" "$ROOT" "$R"
      warn "Browse or enter a custom path instead."
    else
      printf '%s✘ No _CommonRedist-style folder in the game folder.%s\n' "$RED$B" "$R"
      warn "Browse or enter a custom path instead."
    fi
  else
    warn "Couldn't locate the game's install folder."
  fi
  menu=
  for k in "${!FOUND[@]}"; do
    dir=${FOUND[k]#*$'\t'}
    menu+="found:$k"$'\t'"📦 ${dir#"$ROOT"/}   (${FOUND[k]%%$'\t'*} installers)"$'\n'
  done
  if [[ -n $GAMEDIR ]]; then where="the game folder"; else where="your home folder"; fi
  menu+="browse"$'\t'"📂 Browse $where…"$'\n'
  menu+="custom"$'\t'"✏️  Enter a custom path…"
  choice=$(pick_menu "Redist folder" "$menu") || true
  case $choice in
    found:*) FOLDER=${FOUND[${choice#found:}]#*$'\t'}; RECURSIVE=1 ;;
    browse)  FOLDER=$(browse "${GAMEDIR:-$HOME}") && RECURSIVE=1 ;;
  esac
  echo
fi

# ── folder ───────────────────────────────────────────────────────────────────
normalize() {
  local s=$1
  s=${s#"${s%%[![:space:]]*}"}; s=${s%"${s##*[![:space:]]}"}   # trim
  [[ $s == \"*\" || $s == \'*\' ]] && s=${s:1:-1}
  [[ $s == file://* ]] && { s=${s#file://}; s=$(printf '%b' "${s//%/\\x}"); }   # %20 → space
  s=${s//\\ / }
  [[ $s == "~"* ]] && s=$HOME${s:1}
  printf '%s' "$s"
}

find_exes() {
  local depth=(-maxdepth 1)
  ((RECURSIVE)) && depth=()
  find "$1" "${depth[@]}" -type f \( -iname '*.exe' -o -iname '*.msi' \) -print0 2>/dev/null | sort -z -f
}

while :; do
  if [[ -z $FOLDER ]]; then
    read -e -r -p "${B}📁 Folder with the redistributables (Tab completes):${R} " FOLDER || die "Aborted."
  fi
  DIR=$(normalize "$FOLDER"); FOLDER=
  [[ -f $DIR && ${DIR,,} == *.@(exe|msi) ]] && DIR=$(dirname -- "$DIR")
  if [[ ! -e $DIR ]]; then warn "Not found: $DIR"; continue; fi
  if [[ ! -d $DIR ]]; then warn "That's a file, not a folder."; continue; fi
  if [[ ! -r $DIR || ! -x $DIR ]]; then warn "Permission denied. Try: chmod -R u+rX $(printf %q "$DIR")"; continue; fi
  DIR=$(realpath -- "$DIR")
  mapfile -d '' EXES < <(find_exes "$DIR")
  if ((${#EXES[@]} == 0)); then
    if ((!RECURSIVE)); then
      read -r -p "$(warn 'No .exe/.msi files at the top level.') Search sub-folders? [Y/n] " a
      [[ ${a,,} != n* ]] && { RECURSIVE=1; FOLDER=$DIR; }
    else
      warn "No .exe or .msi files anywhere in that folder."
    fi
    continue
  fi
  break
done

silent_flags() {
  local n=${1##*/}; n=${n,,}
  case $n in
    *.msi)                                      echo "/qn /norestart" ;;
    vc_redist[._-]*)                            echo "/install /quiet /norestart" ;;
    vcredist*)                                  echo "/q" ;;
    dxsetup.exe)                                echo "/silent" ;;
    ndp*|dotnet*|netfx*|windowsdesktop-runtime*) echo "/q /norestart" ;;
    oalinst.exe)                                echo "/s" ;;
  esac
}

# ── confirm ──────────────────────────────────────────────────────────────────
line() { printf '%s%s%s\n' "$D" "────────────────────────────────────────────────────────────" "$R"; }
echo; line
printf '  %sGame%s     %s\n'  "$B" "$R" "$NAME"
printf '  %sAppID%s    %s%s%s\n' "$B" "$R" "$CYN" "$APPID" "$R"
N_MSI=0; for e in "${EXES[@]}"; do [[ ${e,,} == *.msi ]] && N_MSI=$((N_MSI + 1)); done
printf '  %sFiles%s    %d .exe, %d .msi\n' "$B" "$R" $((${#EXES[@]} - N_MSI)) "$N_MSI"
printf '  %sFolder%s   %s\n' "$B" "$R" "$DIR"
printf '  %sMode%s     %s%s%s\n' "$B" "$R" "$( ((SILENT)) && echo silent || echo interactive)" \
  "$( ((DRYRUN)) && echo ', dry run')" "$( ((RECURSIVE)) && echo ', recursive')"
line
for e in "${EXES[@]}"; do
  f=$( ((SILENT)) && silent_flags "$e")
  printf '  • %s %s%s%s\n' "${e#"$DIR"/}" "$D" "$f" "$R"
done
line
if [[ $KIND == flatpak ]]; then
  warn "Flatpak Protontricks can only read folders it was granted. If installs fail, run:"
  printf '    %sflatpak override --user --filesystem=%q:ro %s%s\n' "$CYN" "$DIR" "$FLATPAK_ID" "$R"
fi
# ── Steam launch options ─────────────────────────────────────────────────────
# perl helper: get/set LaunchOptions in localconfig.vdf (text) or shortcuts.vdf
# (binary), editing only that value in place; also does the merge. Inputs come
# from RI_* environment variables so nothing needs shell quoting.
VDF_PERL='
use strict; use warnings;
my ($op, $kind, $file) = @ENV{qw(RI_OP RI_KIND RI_FILE)};
if ($op eq "merge") {
  my ($cur, $new) = map { my $x = $_ // ""; $x =~ s/^\s+|\s+$//g; $x } @ENV{qw(RI_CUR RI_VALUE)};
  if ($ENV{RI_REPLACE} || $cur eq "" || index($new, "%command%") < 0) { print $new; exit }
  if ($cur eq $new) { print $cur; exit }
  my ($prefix, $suffix) = split /%command%/, $new, 2;
  for my $var ($prefix =~ /(?:^|\s)(\w+)=/g) { $cur =~ s/(?:^|(?<=\s))\Q$var\E=(?:"[^"]*"|\S*)\s*//g }
  $cur =~ s/^\s+|\s+$//g;
  my $rest = index($cur, "%command%") >= 0 ? $cur : "%command% $cur";
  $rest =~ s/\s+$//;
  print join " ", grep { length } map { my $x = $_ // ""; $x =~ s/^\s+|\s+$//g; $x } $prefix, $rest, $suffix;
  exit;
}
open my $fh, "<:raw", $file or die "$file: $!\n"; my $d = do { local $/; <$fh> }; close $fh;
my ($found, $cur, $s, $e, $ins, $text) = (0, "");
if ($kind eq "localconfig") {
  my @want = (qw(userlocalconfigstore software valve steam apps), $ENV{RI_APPID});
  my (@path, $pending, $apps_close);
  while ($d =~ /\G(?:"((?:[^"\\]|\\.)*)"|([{}])|\/\/[^\n]*|\s+)/gcs) {
    my ($str, $br, $end) = ($1, $2, pos $d);
    my $at = sub { my $n = shift; @path == $n && join("\0", map lc, @path) eq join("\0", @want[0 .. $n - 1]) };
    if (defined $str) {
      if (!defined $pending) { $pending = $str; next }
      if ($at->(6) && lc $pending eq "launchoptions") {
        ($found, $cur, $s, $e) = (1, $str, $end - length($str) - 1, $end - 1);
        $cur =~ s/\\(.)/$1/g;
      }
      undef $pending;
    } elsif (defined $br && $br eq "{") { push @path, $pending // ""; undef $pending }
    elsif (defined $br) {
      my $line = rindex($d, "\n", $end - 1) + 1;
      if ($at->(6)) { $ins //= $line; $text //= ("\t" x 6) . "\"LaunchOptions\"\t\t\"%s\"\n" }
      if ($at->(5)) { $apps_close = $line }
      pop @path;
    }
  }
  if (!defined $ins && defined $apps_close) {   # app block missing: create it
    ($ins, $text) = ($apps_close, ("\t" x 5) . "\"$ENV{RI_APPID}\"\n" . ("\t" x 5) . "{\n"
                     . ("\t" x 6) . "\"LaunchOptions\"\t\t\"%s\"\n" . ("\t" x 5) . "}\n");
  }
} else {
  my $p = 0; my %size = (2 => 4, 3 => 4, 7 => 8, 10 => 8);
  my $cstr = sub { my $z = index($d, "\0", $p); die "bad VDF\n" if $z < 0; my $x = substr($d, $p, $z - $p); $p = $z + 1; $x };
  my $node; $node = sub {
    my $lvl = shift; my %f;
    while ($p < length $d) {
      my $t = ord substr($d, $p++, 1);
      if ($t == 8) {
        if ($lvl == 2 && !$found && ((defined $f{id} && $f{id} eq $ENV{RI_APPID}) || (defined $f{name} && $f{name} eq $ENV{RI_NAME}))) {
          $found = 1;
          if (defined $f{s}) { ($cur, $s, $e) = @f{qw(v s e)} } else { ($ins, $text) = ($p - 1, "\x01LaunchOptions\x00%s\x00") }
        }
        return;
      }
      my $k = lc $cstr->();
      if ($t == 0) { $node->($lvl + 1) }
      elsif ($t == 1) { my $vs = $p; my $v = $cstr->(); next unless $lvl == 2;
        $f{name} = $v if $k eq "appname"; @f{qw(v s e)} = ($v, $vs, $p - 1) if $k eq "launchoptions" }
      elsif (exists $size{$t}) { my $raw = substr($d, $p, $size{$t}); $p += $size{$t}; $f{id} = unpack("V", $raw) if $lvl == 2 && $k eq "appid" }
      else { die "unknown VDF type $t\n" }
    }
  };
  $node->(0);
}
if ($op eq "get") { print "FOUND\t$cur" if $found || ($kind eq "localconfig" && defined $ins); exit }
my $val = $ENV{RI_VALUE};
$val =~ s/(["\\])/\\$1/g if $kind eq "localconfig";
if (defined $s) { substr($d, $s, $e - $s) = $val }
elsif (defined $ins) { substr($d, $ins, 0) = sprintf $text, $val }
else { die "game not found in $file\n" }
my $mode = (stat $file)[2] & 07777;
open my $out, ">:raw", "$file.redinstaller-tmp" or die "$!\n"; print $out $d; close $out or die "$!\n";
chmod $mode, "$file.redinstaller-tmp"; rename "$file.redinstaller-tmp", $file or die "$!\n";
'
vdf() { perl -e "$VDF_PERL"; }

# Prints "KIND<TAB>FILE<TAB>CURRENT" for every place this game's options live.
launch_targets() {
  local root f user id
  while IFS= read -r root; do
    if [[ $NAME == "Non-Steam shortcut"* ]] || ((APPID >= 2147483648)); then
      for f in "$root"/userdata/*/config/shortcuts.vdf; do
        [[ -f $f ]] || continue
        RI_OP=get RI_KIND=shortcuts RI_FILE=$f RI_APPID=$APPID RI_NAME=${NAME#Non-Steam shortcut: } vdf 2>/dev/null |
          sed -n "s|^FOUND\t|shortcuts\t$f\t|p"
      done
    else
      # logged-in account: MostRecent in loginusers.vdf, else newest localconfig.vdf
      id=$(awk -F'"' '/^\t"[0-9]+"/ {sid=$2} /"MostRecent"[[:space:]]+"1"/ {print sid}' "$root/config/loginusers.vdf" 2>/dev/null | head -n1)
      [[ -n $id ]] && user=$root/userdata/$((id - 76561197960265728)) || user=
      [[ -n $user && -f $user/config/localconfig.vdf ]] ||
        user=$(ls -t "$root"/userdata/*/config/localconfig.vdf 2>/dev/null | grep -v '/userdata/0/' | head -n1 | sed 's|/config/localconfig.vdf$||')
      [[ -n $user ]] || continue
      f=$user/config/localconfig.vdf
      RI_OP=get RI_KIND=localconfig RI_FILE=$f RI_APPID=$APPID vdf 2>/dev/null | sed -n "s|^FOUND\t|localconfig\t$f\t|p"
    fi
  done < <(steam_roots)
}

steam_running() { pgrep -x steam >/dev/null; }
shutdown_steam() {
  if command -v steam >/dev/null; then setsid steam -shutdown </dev/null >/dev/null 2>&1 &
  elif command -v flatpak >/dev/null; then setsid flatpak run com.valvesoftware.Steam -shutdown </dev/null >/dev/null 2>&1 &
  else return 1; fi
  local i
  for ((i = 0; i < 45; i++)); do steam_running || { sleep 2; return 0; }; sleep 1; done
  return 1
}

LAUNCH_PLAN=()   # "KIND<TAB>FILE<TAB>NEW"
if [[ -z $LAUNCH ]]; then
  read -r -p "${B}Also set Steam launch options for ${NAME}? [y/N] ${R}" a
  [[ ${a,,} == y* ]] && LAUNCH=1
fi
if [[ $LAUNCH == 1 ]]; then
  if ! command -v perl >/dev/null; then
    warn "perl is needed to edit Steam's config. Set it by hand instead: Steam → ${NAME} → Properties → Launch options:"
    printf '    %s\n' "$LAUNCH_VALUE"
  else
    read -e -r -i "$LAUNCH_VALUE" -p "${B}Launch options:${R} " LAUNCH_VALUE
    if ((!LAUNCH_REPLACE)); then
      read -r -p "Merge with any existing options (keeps them) or replace? [M/r] " a
      [[ ${a,,} == r* ]] && LAUNCH_REPLACE=1
    fi
    mapfile -t TARGETS < <(launch_targets)
    ((${#TARGETS[@]})) || warn "This game isn't in your Steam config; launch options skipped."
    for t in "${TARGETS[@]}"; do
      IFS=$'\t' read -r kind file current <<<"$t"
      new=$(RI_OP=merge RI_CUR=$current RI_VALUE=$LAUNCH_VALUE RI_REPLACE=$LAUNCH_REPLACE vdf)
      printf '  %s%s%s %s(%s)%s\n    now  %s\n' "$B" "$([[ $kind == localconfig ]] && echo 'Steam game' || echo 'non-Steam shortcut')" "$R" "$D" "$file" "$R" "${current:-${D}(none)${R}}"
      if [[ $new == "$current" ]]; then
        printf '    %s✔ already set; nothing to change%s\n' "$GRN" "$R"
      else
        printf '    %snew%s  %s\n' "$GRN" "$R" "$new"
        LAUNCH_PLAN+=("$kind"$'\t'"$file"$'\t'"$new")
      fi
    done
  fi
  line
fi

read -r -p "${B}Install ${#EXES[@]} file(s) into ${NAME}? [Y/n] ${R}" a
[[ ${a,,} == n* ]] && die "Cancelled."

LAUNCH_LOG=()
if ((${#LAUNCH_PLAN[@]})); then
  apply=1
  if ((DRYRUN)); then
    apply=0; LAUNCH_LOG+=("✔ Launch options (dry run, not written)")
  elif steam_running; then
    warn "Steam is running. It rewrites its config when it exits, so the change would be lost."
    read -r -p "Close Steam now (also closes any game running through Steam)? [y = close / s = skip] " a
    if [[ ${a,,} == y* ]]; then
      info "Waiting for Steam to exit…"
      if shutdown_steam; then ok "Steam closed; start it again when you're done."
      else apply=0; LAUNCH_LOG+=("✘ Launch options: Steam didn't exit in time; not written"); fi
    else
      apply=0; LAUNCH_LOG+=("⚠ Launch options: skipped (Steam was running)")
    fi
  fi
  if ((apply)); then
    for t in "${LAUNCH_PLAN[@]}"; do
      IFS=$'\t' read -r kind file new <<<"$t"
      backup=$file.redinstaller-$(date +%Y%m%d-%H%M%S).bak
      n=1; while [[ -e $backup ]]; do n=$((n + 1)); backup=$file.redinstaller-$(date +%Y%m%d-%H%M%S)-$n.bak; done
      if cp -p -- "$file" "$backup" && err=$(RI_OP=set RI_KIND=$kind RI_FILE=$file RI_APPID=$APPID RI_NAME=${NAME#Non-Steam shortcut: } RI_VALUE=$new vdf 2>&1); then
        ok "Launch options set → $new ${D}(backup: ${backup##*/})${R}"
        LAUNCH_LOG+=("✔ Launch options set → $new (backup: $backup)")
      else
        printf '%s✘ Couldn'"'"'t write %s: %s%s\n' "$RED" "$file" "$err" "$R"
        LAUNCH_LOG+=("✘ Launch options: couldn't write $file")
      fi
    done
  fi
fi
echo

# ── install loop ─────────────────────────────────────────────────────────────
((VERBOSE)) || export WINEDEBUG=${WINEDEBUG:--all}
INTERRUPTED=0
trap 'INTERRUPTED=1' INT

bar() { # bar DONE TOTAL
  local w=30 f=$(($1 * 30 / $2)) full empty
  printf -v full '%*s' "$f" ''; printf -v empty '%*s' $((w - f)) ''
  printf '%s%s%s%s%s' "$GRN" "${full// /█}" "$D" "${empty// /░}" "$R"
}

OKS=() FAILS=() SKIPS=()
total=${#EXES[@]} i=0
for exe in "${EXES[@]}"; do
  i=$((i + 1)); rel=${exe#"$DIR"/}
  read -r -a flags <<<"$( ((SILENT)) && silent_flags "$exe")"
  if [[ ${exe,,} == *.msi ]]; then
    # Wine can't run an .msi directly; hand it to msiexec inside the prefix.
    cmd=("${PT_LIST[@]}" -c "wine msiexec /i $(printf '%q' "$exe")${flags[*]:+ ${flags[*]}}" "$APPID")
  else
    cmd=("${PT_LAUNCH[@]}" --appid "$APPID" "$exe" "${flags[@]}")
  fi
  printf '\n%s %s[%d/%d]%s %s%s%s\n' "$(bar $((i - 1)) "$total")" "$B" "$i" "$total" "$R" "$BLU$B" "$rel" "$R"
  printf '  %s$ %s%s\n' "$D" "$(printf '%q ' "${cmd[@]}")" "$R"

  start=$SECONDS
  if ((DRYRUN)); then
    rc=0
  else
    (cd -- "$(dirname -- "$exe")" && exec "${cmd[@]}" </dev/null) 2>&1 | sed -u "s/^/  ${D}│${R} /"
    rc=${PIPESTATUS[0]}
  fi
  secs=$((SECONDS - start))

  if ((INTERRUPTED)); then
    INTERRUPTED=0
    SKIPS+=("$rel")
    warn "Skipped $rel"
    read -r -p "Continue with the remaining installers? [Y/n] " a
    if [[ ${a,,} == n* ]]; then
      for rest in "${EXES[@]:i}"; do SKIPS+=("${rest#"$DIR"/}"); done
      break
    fi
    continue
  fi
  case $rc in
    0)    ok   "$rel ${D}(${secs}s)${R}"; OKS+=("$rel") ;;
    1638) ok   "$rel ${D}(newer version already installed, ${secs}s)${R}"; OKS+=("$rel") ;;
    3010|1641) ok "$rel ${D}(reboot requested, ignored, ${secs}s)${R}"; OKS+=("$rel") ;;
    127)  printf '%s✘%s %s: launcher not found\n' "$RED" "$R" "$rel"; FAILS+=("$rel (launcher not found)") ;;
    *)    printf '%s✘%s %s %s(exit %d, %ss)%s\n' "$RED" "$R" "$rel" "$D" "$rc" "$secs" "$R"; FAILS+=("$rel (exit $rc)") ;;
  esac
done
trap - INT
printf '\n%s %s[%d/%d]%s\n' "$(bar "$total" "$total")" "$B" "$total" "$total" "$R"

# ── summary ──────────────────────────────────────────────────────────────────
echo; line
printf '  %sSummary for %s (%s)%s\n' "$B" "$NAME" "$APPID" "$R"
line
for x in "${OKS[@]}";   do printf '  %s✔%s %s\n' "$GRN" "$R" "$x"; done
for x in "${FAILS[@]}"; do printf '  %s✘%s %s\n' "$RED" "$R" "$x"; done
for x in "${SKIPS[@]}"; do printf '  %s⏭%s %s\n' "$YEL" "$R" "$x"; done
for x in "${LAUNCH_LOG[@]}"; do printf '  %s\n' "$x"; done
line
printf '  %s%d succeeded%s · %s%d failed%s · %s%d skipped%s\n\n' \
  "$GRN" "${#OKS[@]}" "$R" "$RED" "${#FAILS[@]}" "$R" "$YEL" "${#SKIPS[@]}" "$R"
if ((${#FAILS[@]})); then
  printf '%sTip: rerun with --verbose for Wine debug output, or retry one by hand:\n  %s --appid %s /path/to/file.exe%s\n' \
    "$D" "${PT_LAUNCH[*]}" "$APPID" "$R"
  exit 1
fi
