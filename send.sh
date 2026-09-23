#!/usr/bin/env bash
# Chappe v1 sender. Requires Bash 4+ and GNU coreutils; UTF-8 terminal.
set -euo pipefail
export LC_ALL=C

usage() {
    cat <<'EOF'
Usage: bash send.sh [--mode sextant|quadrant]
       [--interval SECONDS] [--no-wait] [--dump] FILE
FILE may be - for stdin. Default: sextant, 0.3 seconds per screen.
--interval sets the minimum hold; next-screen preparation runs during that hold.
--dump emits successive 80x24 screens as UTF-8 text, separated by form feed.
EOF
}
die() {
    printf 'Error: %s\n' "$*" >&2
    exit 1
}

((BASH_VERSINFO[0] >= 4)) || die 'Bash 4 or newer is required'
readonly SCREEN_ROWS=24 SCREEN_COLS=80 BODY_ROWS=18 BODY_COLS=76
readonly BODY_CELLS=$((BODY_ROWS * BODY_COLS))
mode=sextant
interval=0.3
no_wait=0
dump=0
source=
while (($#)); do
    case $1 in
        --mode)
            (($# >= 2)) || die '--mode requires a value'
            mode=$2
            shift 2
            ;;
        --interval)
            (($# >= 2)) || die '--interval requires a value'
            interval=$2
            shift 2
            ;;
        --no-wait) no_wait=1; shift;;
        --dump) dump=1; shift;;
        -h|--help) usage; exit 0;;
        --)
            shift
            [[ $# == 1 && -z $source ]] || die 'Expected one input file after --'
            source=$1
            shift
            ;;
        -|[!-]*)
            [[ -z $source ]] || die 'Expected only one input file'
            source=$1
            shift
            ;;
        *) die "Unknown option: $1";;
    esac
done
[[ $mode == sextant || $mode == quadrant ]] || die '--mode must be sextant or quadrant'
[[ $interval =~ ^([0-9]+([.][0-9]+)?|[.][0-9]+)$ ]] || die '--interval must be a nonnegative number'
[[ -n $source ]] || { usage >&2; exit 1; }
[[ $source == - || (-r $source && ! -d $source) ]] || die "Cannot read input: $source"
# GNU date supplies nanosecond timestamps; Bash keeps the scheduling arithmetic
# integral. Dump mode has no clock or sleep overhead.
interval_ns=0
next_display_ns=0
if ((!dump)); then
    printf -v interval_ns '%.0f' "${interval}e9"
    # Leave room for the current epoch timestamp in signed 64-bit arithmetic.
    [[ ${#interval_ns} -le 18 ]] || die '--interval is too large'
fi

# Bit 0 is upper left; positions run left-to-right, then top-to-bottom.
# Literal array entries avoid locale-dependent UTF-8 substring operations.
quadrants=(' ' ▘ ▝ ▀ ▖ ▌ ▞ ▛ ▗ ▚ ▐ ▜ ▄ ▙ ▟ █)
sextants=(' ' 🬀 🬁 🬂 🬃 🬄 🬅 🬆 🬇 🬈 🬉 🬊 🬋 🬌 🬍 🬎 🬏 🬐 🬑 🬒 🬓 ▌ 🬔 🬕 🬖 🬗 🬘 🬙 🬚 🬛 🬜 🬝 🬞 🬟 🬠 🬡 🬢 🬣 🬤 🬥 🬦 🬧 ▐ 🬨 🬩 🬪 🬫 🬬 🬭 🬮 🬯 🬰 🬱 🬲 🬳 🬴 🬵 🬶 🬷 🬸 🬹 🬺 🬻 █)
if [[ $mode == sextant ]]; then
    glyphs=("${sextants[@]}")
    bits=6
else
    glyphs=("${quadrants[@]}")
    bits=4
fi
capacity=$((BODY_CELLS * bits / 8))
base64_alphabet=ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/
declare -A values=()
for ((i=0;i<64;i++)); do values[${base64_alphabet:i:1}]=$i; done
active=0 producer=
cleanup() {
    exec 3<&-
    if [[ -n $producer ]]; then
        kill "$producer" 2>/dev/null || :
        wait "$producer" 2>/dev/null || :
    fi
    if ((active)); then printf '\033[0m\033[?7h\033[?25h\033[?1049l' >&4; fi
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM HUP
if ((!dump)); then
    exec 4>/dev/tty
    read -r rows cols < <(stty size < /dev/tty)
    ((rows>=SCREEN_ROWS && cols>=SCREEN_COLS)) || die 'Need an 80x24 terminal'
    printf '%s mode; %s bytes/page; minimum %s seconds/page.\n' "$mode" "$capacity" "$interval" >&2
    if ((!no_wait)); then
        printf 'Start recording, then press Enter: ' >&4
        read -r _ < /dev/tty
    fi
    active=1
    printf '\033[?1049h\033[?25l\033[?7l\033[0;97;40m\033[2J' >&4
fi

border= reference=
for ((i=0;i<SCREEN_COLS;i++)); do border+=█; done
for ((i=0;i<38;i++)); do reference+='█ '; done

# Header: 32 bytes, encoded as 64 quadrant glyphs, then 12 reference cells.
# CB01 | kind (C/D/E) | bits | page u64be | length u16be | SHA256 prefix (16 bytes).
header() {
    local kind=$1 length=$2 encoded=$3 prefix digest hex i value
    printf -v prefix '43423031%02x%02x%016x%04x' "'$kind" "$bits" "$page" "$length"
    digest=$({ printf '%s' "$prefix"; printf '%s' "$encoded" | base64 -d; } | sha256sum)
    hex=$prefix${digest:0:32}
    header_row=
    for ((i=0;i<${#hex};i++)); do
        value=$((16#${hex:i:1})); header_row+=${quadrants[value]}
    done
    header_row+='█ █ █ █ █ █ '
}

wait_for_display() {
    local now remaining delay
    if ((next_display_ns == 0)); then return; fi
    now=$(date +%s%N)
    remaining=$((next_display_ns - now))
    if ((remaining > 0)); then
        # A backward wall-clock adjustment must not create an unbounded pause.
        if ((remaining > interval_ns)); then remaining=$interval_ns; fi
        printf -v delay '%d.%09d' "$((remaining / 1000000000))" "$((remaining % 1000000000))"
        sleep "$delay"
    fi
}

show() {
    local row content position screen=
    for ((row=0;row<SCREEN_ROWS;row++)); do
        case $row in
            0|23) content=$border;;
            1|2|22) content="█ $header_row █";;
            3) content="█ $reference █";;
            *) content="█ ${body_rows[row-4]} █";;
        esac
        if ((dump)); then screen+=$content$'\n'
        else printf -v position '\033[%d;1H' "$((row+1))"; screen+=$position$content; fi
    done
    if ((dump)); then
        printf '%s\f' "$screen"
    else
        # Finish the entire next screen before waiting out the previous hold.
        wait_for_display
        printf '%s' "$screen" >&4
        if ((interval_ns > 0)); then
            # Rebase after every write: a late page never triggers catch-up.
            next_display_ns=$(date +%s%N)
            next_display_ns=$((next_display_ns + interval_ns))
        fi
    fi
}

# Calibration displays every mask repeatedly; it has no source payload.
page=0
header C 0 ''
body_rows=()
for ((r=0;r<BODY_ROWS;r++)); do
    line=
    for ((c=0;c<BODY_COLS;c++)); do line+=${glyphs[(r*BODY_COLS+c)%(1<<bits)]}; done
    body_rows[r]=$line
done
for ((repeat=0;repeat<4;repeat++)); do show; done

# Base64 is binary-safe and streams through bounded pipes. Read one page only.
# One tracked process reads the input; its failure must not produce an end marker.
exec 3< <(exec base64 -w0 -- "$source")
producer=$!
encoded_capacity=$((capacity/3*4))
total=0
while :; do
    encoded=
    if IFS= read -r -N "$encoded_capacity" encoded <&3; then :
    else [[ -n $encoded ]] || break; fi
    length=$((${#encoded}/4*3))
    [[ $encoded != *= ]] || length=$((length-1))
    [[ $encoded != *== ]] || length=$((length-1))
    header D "$length" "$encoded"
    # Consume Base64 sextets MSB-first, then emit 6- or 4-bit masks MSB-first.
    body_rows=(); line=; cells=0; reservoir=0; available=0
    symbols=$(((length*8+bits-1)/bits))
    for ((i=0;i<${#encoded};i++)); do
        char=${encoded:i:1}; [[ $char != = ]] || break
        reservoir=$(((reservoir<<6)|values[$char])); available=$((available+6))
        while ((available>=bits && cells<symbols)); do
            available=$((available-bits))
            value=$(((reservoir>>available)&((1<<bits)-1)))
            line+=${glyphs[value]}; cells=$((cells+1))
            if ((cells%BODY_COLS==0)); then body_rows+=("$line"); line=; fi
        done
        reservoir=$((reservoir&((1<<available)-1)))
    done
    # Base64 includes enough zero pad bits to complete every final mask.
    while ((cells<BODY_CELLS)); do
        line+=' '; cells=$((cells+1))
        if ((cells%BODY_COLS==0)); then body_rows+=("$line"); line=; fi
    done
    show
    total=$((total+length)); page=$((page+1))
done
exec 3<&-
status=0
wait "$producer" || status=$?
producer=
((status == 0)) || die 'Input encoding failed; transfer has no end marker'
header E 0 ''
body_rows=()
printf -v blank '%76s' ''
for ((r=0;r<BODY_ROWS;r++)); do body_rows[r]=$blank; done
for ((repeat=0;repeat<3;repeat++)); do show; done
wait_for_display
printf 'Sent %s bytes in %s data pages (%s).\n' "$total" "$page" "$mode" >&2
