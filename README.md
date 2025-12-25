# PDFest - PDF Reader with TTS

A PDF reader with text-to-speech, continuous scrolling, and library management.

## Features

- 📖 Continuous scroll with lazy loading
- 🔊 Text-to-speech with sentence highlighting
- 📚 Library management with reading progress
- 🔍 Zoom in/out (0.5x - 4.0x)
- 📑 Table of contents sidebar (resizable)
- 💾 Auto-save reading position per book
- 📏 Configurable header/footer margins for TTS
- 🔗 Clickable links (opens in browser)
- 📋 Text selection and copy (Ctrl+C)

## User Guide

### TTS Playback

| Button | Action |
|--------|--------|
| **Play/Pause** | Start/stop reading from current position |
| **>> Next Sent** | Skip to next sentence |
| **<< Prev Sent** | Go back to previous sentence |

**Behavior:**
- Playback starts from the **visible page**, not from the beginning
- Sentences are highlighted in **yellow** as they're read
- You can **rapidly skip** forward/backward - old audio is cancelled immediately
- Audio is pre-cached for smooth playback

### TTS Margins

Click **📏 Margins** to exclude header/footer regions from reading:
- Slide to adjust header (top) and footer (bottom) margins
- Red overlay shows the excluded zones
- Click **Apply** to save - re-analyzes text immediately

### Voice Selection

Click **🔊 Voice** to choose from all available Edge TTS voices:
- Voices are organized by language
- Your selection is saved

### Text Selection

- Click and drag to select text
- **Blue highlight** shows selection
- Press **Ctrl+C** to copy

### Links

- Click on any link in the PDF
- External URLs open in your browser
- Internal links (TOC references) jump to that page

## Development

**Run from source:**
```bash
uv run main.py
```

## Building

**Install PyInstaller (first time only):**
```bash
uv add pyinstaller --dev
```

**Build the binary:**
```bash
uv run pyinstaller --onefile --windowed --name pdfest \
  --hidden-import='PIL._tkinter_finder' \
  main.py
```

**Output:** `dist/pdfest`

## Installation

**Install system-wide (optional):**
```bash
sudo cp dist/pdfest /usr/local/bin/
```

Then run from anywhere: `pdfest`

## Data Location

- Library database: `~/.local/pdfest/library.db`
- Your reading progress, zoom, and settings persist across updates
