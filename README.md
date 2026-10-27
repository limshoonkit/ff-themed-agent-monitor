# Agent Monitor

A small Linux desktop window that shows one animated card per active Claude Code or Codex chat. Cards appear and disappear with sessions, show what the agent is doing, and can use a different avatar for each chat. Ordinary playback uses local GIFs and does not call a model.

![The monitor window: usage bars for Anthropic and OpenAI, six chat cards changing state, the help panel, and the avatar picker](docs/demo.gif)

The preview is recorded from the real window, using placeholder chats, a placeholder account and made-up usage numbers. It shows six chats changing state, the `?` help panel, and a chat switching to a different avatar in the picker. The sprite GIFs, source sheets and MP3 themes are excluded from this repository; supply your own assets before running the window.

## What it shows

- Claude Code in VS Code terminals and Codex chats in the desktop app or VS Code extension.
- States such as thinking, reading, using a tool, delegating, waiting for permission, ready, and error.
- A stable avatar choice per chat, with optional randomized working scenes and an idle scene. Click the name under a card's avatar to open the picker and choose a different character for that chat.
- Account usage per provider, as two bars each: a clock for the 5-hour window and a calendar for the weekly one, with the share left and the time until reset.
- Per-chat details: session age, active time, prompt and tool counts, context used, tokens in and out, model, and calls. The lines button switches between full and compact cards. Prompt and tool contents are not stored by the monitor.
- A `?` help panel that explains every state icon, the usage bars, and the card details.

Codex chats can be detected from their local rollout metadata before hooks are trusted. Detailed Codex tool and permission states require approving the configured hooks in Codex's **Hooks need review** prompt or `/hooks`.

## Requirements

Linux, Python 3, Tkinter, and Pillow. The GUI needs a desktop display. The Claude Code and Codex integrations use local session files and lifecycle hooks; install only the integration you use.

```bash
python3 -m pip install --user Pillow
python3 scripts/monitor.py install-hooks        # Claude Code
python3 scripts/monitor.py install-codex-hooks  # Codex; then approve in Codex
python3 scripts/monitor.py start
python3 scripts/monitor.py status
```

`install-hooks` and `install-codex-hooks` back up and merge the respective existing settings. `stop` closes the monitor. `uninstall-hooks` and `uninstall-codex-hooks` remove only this monitor's hook entries. The `SKILL.md` file describes the full command set and state mapping.

## Avatars

Copy your GIFs into `assets/<name>/` or register a separate local folder:

```bash
python3 scripts/monitor.py add-avatar "My Avatar" /absolute/path/to/gifs
```

Each avatar needs the five base GIFs listed in [assets/README.md](assets/README.md), or a complete `animations.json` mapping. `variants.json` may list extra working scenes. The personal development sprites are ignored by Git; a fresh clone needs its own local GIF assets. The local set is Tifa, Rinoa, Yuna and Lightning.

## Music

Music files go in the character's folder and are ignored by Git. A `music.json` there names them by mood:

```json
{
  "field": ["Rinoa's Theme.mp3", "Balamb Garden.mp3", "Blue Fields.mp3"],
  "battle": ["Don't be Afraid.mp3", "Man with the Machine Gun.mp3"],
  "boss": ["Force Your Way.mp3", "The Extreme.mp3"],
  "victory": ["Victory Fanfare.mp3"],
  "victory_seconds": 7
}
```

The usage bars set the mood, using the chat's own provider and its lowest window:

| Usage left | Music |
|---|---|
| above 50% | field |
| 20–50% | battle |
| below 20% | boss |

A mood only changes once usage is 5 points past a threshold, so estimated readings near 50% or 20% don't make the music flip back and forth. A character with no tracks for a mood plays the next calmer one.

Music starts once a task has run for 5 seconds, so quick replies stay silent. Music follows the whole turn rather than each state, because a chat switches between thinking and tool use every few seconds. When a task of 30 seconds or more finishes, the victory track plays its opening seconds and fades out; shorter tasks get a chime. Each list shuffles without repeating a track twice in a row. Every track also resumes where it stopped, so a run of short tasks moves through a long track instead of replaying its intro.

`calm_above`, `boss_below`, `music_delay`, `victory_after`, `victory_seconds` and `theme_volume` in `config.json` change the thresholds, timings and volume. `python3 scripts/monitor.py status` shows the playlists each character resolved.

## Development

```bash
python3 -m unittest discover -s tests
```

`tests/gui_smoke.py` also exercises the real Tk window when a display is available. It uses synthetic chats and temporary state.

To rebuild the preview with the local builder (`docs/build_demo.py` is ignored by Git, so it exists only in the original working copy) after supplying the local character GIFs:

```bash
python3 docs/build_demo.py
```

The script runs the real window against temporary state, with seeded placeholder chats, transcripts and usage readings. It makes no hook, network or credential access and plays no sound. Frames are read from the monitor's own X11 windows rather than the screen, so other windows on the desktop never appear in the GIF. It needs an X11 session with a compositor.

The monitor is a local visualization. It does not control either agent or the native ChatGPT Pet. It reads Claude Code's local OAuth credential only to request the same account usage reading Claude Code uses; it never logs or stores that token. Codex usage comes from Codex's own app server, which makes that request with Codex's login, and from Codex's local rollout files; the monitor never reads the OpenAI token.
