# amp — Animated Man Page Pager

**amp** is a terminal pager (similar in spirit to `less`) that can overlay live animations inside rectangular regions of a man page or plain text document.

It is designed to be used as `MANPAGER` (or via `man -P`). Ordinary pagers still show the static placeholder text inside each animation box; only `amp` replaces those regions with a looping typescript or caca-style animation.

Companion tool: **amp-trim** — cleans and bounds `script(1)` recordings so they fit cleanly inside a fixed-size box.

## Features

- Rectangular animation boxes delimited by markers
- Colour / 256-colour / caca-style frames
- Looping by default
- Bottom-aligned display when source content is taller than the box
- Works with `man -P` and as a drop-in `MANPAGER`
- Built-in spinner when animation files are missing
- Search, paging, and help screen (like a simplified `less`)

## Quick start

```bash
# From the amp/ directory
./install.sh                  # print export line for current shell
./install.sh --permanent      # append to ~/.bashrc or ~/.zshrc

# View the sample page
python3 amp.py sample_man.txt
# or
man -P ./amp sample           # after placing a formatted page where man can find it
```

Undo with `./uninstall.sh`.

## Marking an animation box

Place this in your man-page source (or any plain-text document):

```text
This content best viewed with amp.

    <<<ANIM_BOX w=40 h=8 id=A timing=demo.timing typescript=demo.typescript>>>
    [ Animation placeholder – visible in less ]
    [ Replace this text with a short description ]
    [ of the tool session shown by the animation. ]
    <<<END_ANIM_BOX>>>
```

Attributes (space-separated, values may be quoted):

| Attribute     | Meaning                                      |
|---------------|----------------------------------------------|
| `w=N`         | Width in columns                             |
| `h=N`         | Height in rows                               |
| `id=L`        | Single letter A–Z (default: first free)      |
| `timing=path` | `script(1)` timing file                      |
| `typescript=path` | Corresponding typescript                 |
| `divisor=N`   | Speed multiplier (default 1.0)               |
| `loop=0\|1`   | Loop the animation (default 1)               |

Path resolution order:

1. Absolute / `~` path or path relative to the current directory
2. `$AMP_DATA_DIR/` and `$AMP_DATA_DIR/animations/`
3. `$XDG_DATA_HOME/amp/` (or `~/.local/share/amp/`)
4. `/usr/share/amp/`

If both paths are given on the marker, only `MANPAGER` needs to be set.

## Preparing animations with amp-trim

Record a session:

```bash
script -T timing typescript
# … do something interesting …
exit
```

Detect the natural bounding box (especially useful for caca / clear-screen animations):

```bash
amp-trim -t timing -s typescript
```

Trim to a fixed size and strip sequences that would fight the pager:

```bash
amp-trim -t timing -s typescript -W 40 -H 8 \
         -T out.timing -S out.typescript
```

Useful options: `-R N` (shift left by N columns), `-v` (verbose).

`amp-trim` removes clear-screen, cursor-home, and other control sequences that would escape the box while preserving SGR colour codes.

## Directory layout

```
amp/
  amp                 # thin wrapper (execs amp.py)
  amp.py              # main pager
  amp.1               # man page
  install.sh
  uninstall.sh
  sample_man.txt      # demonstration page
  demo.timing
  demo.typescript
  caca_demo.*         # example coloured animation

```

## Requirements

- Python 3.8+
- A terminal that understands ANSI colour and cursor movement
- `script` / `scriptreplay` (for creating source animations)
- Optional: `caca` or any tool that produces clear-screen framed ANSI art

## Keyboard

| Key          | Action              |
|--------------|---------------------|
| `q`          | Quit                |
| `space` / PgDn | Page forward      |
| `b` / PgUp   | Page backward       |
| `j` / `k`    | Line up / down      |
| `g` / `G`    | Top / bottom        |
| `/`          | Search              |
| `n` / `N`    | Next / previous match |
| `h` or `?`   | Help                |

## License

This is free and unencumbered software released into the public domain.

Anyone is free to copy, modify, publish, use, compile, sell, or distribute this software, either in source code form or as a compiled binary, for any purpose, commercial or non-commercial, and by any means.

In jurisdictions that recognize copyright laws, the author or authors of this software dedicate any and all copyright interest in the software to the public domain. We make this dedication for the benefit of the public at large and to the detriment of our heirs and successors.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE AUTHORS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM, OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE SOFTWARE.

For more information, please refer to <https://unlicense.org>

## See also

- `man amp`
- `man amp-trim`
- `man script`
- `man scriptreplay`
