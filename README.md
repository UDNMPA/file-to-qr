# File to QR

**Cold offline storage for encrypted files, backed up as printable QR codes.**

File to QR encrypts a small file with a password, splits the ciphertext into chunks, and turns each chunk into a QR code. Print the codes, store them on paper (safe, fireproof box, split across locations — your call), and you have an air-gapped backup that never touched a network. To restore, scan the codes back in — from image files or straight from your phone's camera — and File to QR reassembles, verifies, and decrypts the original file.

Built for [Termux](https://termux.dev/) on Android. No network calls, ever.

---

## Why

Cloud backups can be breached, subpoenaed, or simply disappear with the provider. A password manager export, a crypto wallet seed, a small set of recovery codes — sometimes you want a backup that:

- physically cannot be exfiltrated over a network, because it's on paper
- survives a dead phone, a wiped laptop, or a defunct cloud account
- is encrypted, so a photo of it lying around isn't enough on its own

File to QR trades convenience for exactly that guarantee. It intentionally caps input files at 52 KB — this is for *small secrets*, not photo albums.

## How it works

```
┌──────────┐   openssl    ┌───────────┐   base64 +   ┌─────────────┐
│  file     │ ──AES-256──▶ │ encrypted │ ──chunk──▶   │  QR code(s)  │
│ (≤ 52 KB) │   CBC+PBKDF2 │   blob    │   + qrencode │  (PNG files)  │
└──────────┘              └───────────┘              └─────────────┘
                                                              │
                                                     print / store
                                                              │
                                                              ▼
┌──────────┐   openssl    ┌───────────┐   zbarimg    ┌─────────────┐
│  file     │ ◀──decrypt── │ encrypted │ ◀──scan──    │  QR code(s)  │
│ restored  │   verified   │   blob    │   (camera or │  (photo or    │
│           │   by hash    │           │    files)    │   PNG files)  │
└──────────┘              └───────────┘              └─────────────┘
```

- **Encryption**: `openssl enc -aes-256-cbc -pbkdf2` — password never touches disk, passed via stdin
- **Integrity**: SHA-256 of the ciphertext is embedded in every chunk and re-verified on reassembly
- **Chunking**: chunk size is tuned so each QR code stays scannable when printed at roughly 5×5 cm
- **Reading**: `zbarimg` decodes by module contrast, so it also works as a general-purpose QR scanner (links, text, colored/custom QR codes) independent of the app's own format

## Features

- 🔐 Encrypt any file up to 52 KB → a set of QR code PNGs
- 📂 Reassemble from a folder of scanned/exported PNGs
- 📷 Reassemble live from the Termux camera — capture multiple QR codes in a single frame, in any order, with a live ASCII preview and progress tracking that survives a restart
- 🔎 General-purpose QR scanner mode (not limited to the app's own format)
- ⚙️ Configurable chunk size and error-correction level
- 🎨 A surprisingly extra terminal UI for a bash-adjacent tool (animated banner, colored ASCII camera preview, progress bars)
- 🚫 Zero network calls — the tool doesn't even import anything that could make one

## Requirements

- Android device with [Termux](https://termux.dev/) (F-Droid build recommended)
- [Termux:API](https://f-droid.org/packages/com.termux.api/) app, only if you want camera scanning (Mode 3)
- Python 3.10+

System packages (inside Termux):

```bash
pkg install openssl-tool qrencode zbar python
pkg install termux-api   # optional, for camera mode
```

Python dependencies:

```bash
pip install -r requirements.txt
```

- `rich` — required, powers the TUI
- `Pillow` — optional, enables the ASCII camera preview

## Usage

```bash
python3 file_to_qr.py
```

You'll land in an interactive menu:

| # | Mode | What it does |
|---|------|---------------|
| 1 | Encrypt file → QR | Pick a file, set a password, get a folder of QR PNGs |
| 2 | Assemble from QR (folder) | Point at a folder of scanned PNGs, decrypt, save |
| 3 | Assemble from QR (camera) | Hold printed codes up to the camera, capture on Enter |
| 4 | Settings | Output folder, chunk size, error-correction level |
| 5 | Scan any QR | General scanner — links, text, any QR code |

### A typical backup flow

1. `Mode 1` → encrypt your file, choose a strong password
2. Print the generated PNGs (or transfer them to a computer to print)
3. Store the printout somewhere safe — ideally more than one somewhere
4. To restore later: `Mode 3` → scan the printouts back with your phone's camera, enter the password

## Configuration

Settings persist to `~/.file_to_qr_config.json`:

```json
{
  "output_dir": "file_to_qr_output",
  "chunk_size": 1300,
  "error_correction": "M"
}
```

- **chunk_size** — raw bytes per QR code before base64 expansion. Larger chunks mean fewer QR codes but denser, harder-to-scan images.
- **error_correction** — `L` / `M` / `Q` / `H`. Higher levels tolerate more print/scan damage but hold less data per code, meaning more codes overall.

## Security notes

- The password is read via `getpass`, piped to `openssl` over stdin, and never written to disk or logs.
- Decrypted output is written to a temp file and copied to your chosen destination — that destination file is **unencrypted on disk**. File to QR warns about this every time; move it somewhere safe promptly.
- AES-256-CBC with PBKDF2 key derivation is what `openssl enc -pbkdf2` provides by default in modern OpenSSL — no custom crypto is implemented by this project.
- This tool has not had a formal security audit. Treat it as a solid DIY layer, not a substitute for professional-grade key management if what you're protecting really matters.

## Limitations

- 52 KB input file cap, by design — this is for small secrets, not general file storage
- Android/Termux-specific (relies on `termux-camera-photo` for camera capture)
- No error handling for exotic printer/scanner distortions beyond QR's built-in error correction

## License

MIT — see [LICENSE](./LICENSE).
