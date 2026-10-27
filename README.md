# Serial Device Test Tools

This folder contains small scripts for configuring serial devices, collecting serial logs, viewing individual logs, and building a timeline report that correlates logs with test spreadsheet timestamps.

## Setup

Use the existing virtual environment from this folder:

```bash
cd /c/Users/dhawn/Desktop/CODE/proofOfConceptSerialLog
source .venv/Scripts/activate
```

If the venv is not active, run scripts with:

```bash
./.venv/Scripts/python.exe script_name.py
```

The scripts expect `pyserial` to be installed in the venv.

In PowerShell, activate the environment with:

```powershell
.\.venv\Scripts\Activate.ps1
```

Or run a script directly with `.\.venv\Scripts\python.exe script_name.py`.

## Collect Serial Logs

Use `serial_at_test.py` to poll one or more devices and save JSON logs.

```bash
python serial_at_test.py --config collection_config.json
```

This reads `collection_config.json`, asks which configured test sequence is being collected, opens all listed COM ports in parallel, sends each command, waits for the `# SGS` prompt, writes a JSON log to `logs/`, generates a matching `_viewer.html`, and opens it in your browser.

For a quick `sta` status check without selecting a test sequence:

```bash
python serial_at_test.py --sta
```

To collect only `con` using the same shortcut behavior:

```bash
python serial_at_test.py --con
```

To choose a sequence without the prompt:

```bash
python serial_at_test.py --sequence "1 - LCC Non-blinking"
```

To list available sequences:

```bash
python serial_at_test.py --config collection_config.json --list-sequences
```

To collect JSON without generating an HTML viewer or opening the browser:

```bash
python serial_at_test.py --config collection_config.json --no-viewer
```

### Collector options

| Option | Default | Behavior |
| --- | --- | --- |
| `--config PATH` | `collection_config.json` beside the script | Read settings from this file. A supplied relative path is resolved from your working folder. |
| `--sta` | Off | Shortcut for `--status-check sta`. Collect `sta` on every configured port without selecting a sequence. |
| `--con` | Off | Shortcut for `--status-check con`. Collect `con` on every configured port without selecting a sequence. |
| `--status-check COMMAND` | Off | Replace the main command list with one command, such as `--status-check con`. No sequence is associated with the run. |
| `--sequence "NAME"` | Interactive selection | Associate the run with an exact, case-sensitive configured name. Quote names containing spaces. |
| `--list-sequences` | Off | Print configured names and sequence descriptions, then exit without opening ports or creating logs. |
| `--no-viewer` | Off | Save JSON only; skip HTML generation and browser opening. |
| `-h`, `--help` | Off | Show command-line help and exit without reading the config or opening ports. |

Choose at most one of `--sta`, `--con`, `--status-check`, `--sequence`, and `--list-sequences`.
Combine your choice with `--config` and/or `--no-viewer` as needed:

```bash
python serial_at_test.py --sta --no-viewer
python serial_at_test.py --config collection_config.json --con
```

All status-check options still run configured `pre_commands` and `post_commands`.
Only the main `commands` list is replaced. With your current empty pre/post lists,
`--sta` sends just `sta`, and `--con` sends just `con`.

Test sequences are descriptive metadata: selecting one saves its name and
current/time description in the log. The collector does not apply that waveform
or execute the sequence text. If sequences are absent or empty, normal collection
runs without a sequence prompt. At the prompt, enter the displayed menu number or
the exact name; the menu number is its position, which may differ from a number
included in the sequence name.

The sequence named `Status Check` in the current config is a normal collection
label. Selecting it runs the full configured command list; use `--sta` to collect
only the status command plus any pre/post commands.

### `collection_config.json`

| Field | Required / default | Accepted value and purpose |
| --- | --- | --- |
| `ports` | Required | Nonempty list of unique port names, such as `["COM11", "COM12"]`. Names cannot be blank or have surrounding whitespace; duplicates are checked without regard to case. |
| `commands` | Required for normal collection | Nonempty list of ASCII serial commands. Current config: `con`, `sta`, `eve`, `b`. Status checks supply their own main command. |
| `test_suite` | `"Serial Log Viewer"` | Nonblank title saved in logs and shown in viewers. |
| `test_sequences` | `{}` | Object mapping nonblank names to nonblank sequence descriptions. `null` also means no sequences. Different names may share the same description. |
| `baud` | `4096` | Positive integer baud rate. Your current config omits this field and uses the default. |
| `timeout` | `10.0` | Positive, finite number of seconds to wait for the prompt after each command. Also bounds serial writes and the wait for all ports to finish startup. |
| `prompt_prefix` | `"# SGS"` | Nonblank prompt text. A response line starting with this text, after leading whitespace, marks completion. |
| `line_ending` | `"cr"` | `none`, `cr`, `lf`, or `crlf` (case-insensitive). Appended to each command. |
| `log_dir` | `"logs"` | Nonblank output path. Relative paths resolve from the config file's folder; absolute paths are used directly. |
| `pre_commands` | `[]` | ASCII commands sent before the main commands. |
| `post_commands` | `[]` | ASCII commands sent after the main commands, if earlier commands succeeded. |

Commands cannot include embedded carriage returns or newlines; use `line_ending`
to supply the terminator. An empty command string is allowed to send just the
terminator. The collector sends the command strings as provided; it does not
enforce read-only device behavior.

### Collection behavior and log records

Each port opens with 8 data bits, no parity, and 1 stop bit. Input and output
buffers are cleared, then workers wait for all ports to be ready before sending
commands. This coordinates the start; it does not guarantee simultaneous sends.
An open/reset failure or startup wait timeout cancels the shared start. Once
collection begins, an error or missing prompt stops further commands on that port;
other ports continue. Post commands are skipped on a port after an earlier failure.

The collector exits with `0` for a successful run or sequence listing, `1` for a
config/output/collection failure, and `2` for invalid CLI arguments. Viewer failures
produce a warning after JSON has been saved and do not change the collection result.

Command records include `status` (`OK`, `TIMEOUT`, or `ERROR`), phase (`pre`,
`test`, or `post`; setup failures may use `open` or `setup`), response byte count,
elapsed seconds, cleaned response lines, and parsed fields. The main phase remains
named `test` for both normal collections and status checks.

New command records use `sent_at` and `completed_at` for explicit timing.
`timestamp` equals `completed_at` for compatibility with existing viewers. Older
logs used send time for exceptions and completion time for normal responses.
Times include the local timezone offset; elapsed time includes sending the command
and waiting for its response.

Response cleaning removes blank lines, prompt lines, and lines containing `>>`.
It strips surrounding whitespace, so `response_text` is cleaned evidence rather
than an exact byte-for-byte transcript. Parsed fields split on the first colon;
repeated keys become lists. A `* ...` continuation following `State` supplies the
displayed state.

### Test definitions and results

`collection_config.json` holds device settings and descriptive test definitions.
Actual test results, expected outcomes, per-device results, and notes currently
come from `condensedTestResults.csv` for the timeline report.

`sequence_to_json.py` provides text entry for sequence definitions:

```text
SOTF = 200ms 500a 10s 0a
```

```bash
python sequence_to_json.py example_sequences.txt
```

This creates `example_sequences.json`; it does not update the collection config.
The converter rejects duplicate normalized sequences and zero durations, while
the collector permits repeated descriptions and treats sequence text as metadata.
Some existing definitions therefore cannot be passed through that converter unchanged.

### Verify collector changes

Run the regression checks with the existing environment:

```powershell
.\.venv\Scripts\python.exe -m unittest -v test_serial_at_test
```

These checks use simulated serial ports and temporary output folders. They cover
CLI modes, config validation, startup cancellation, command errors/timeouts,
log timing fields, and viewer generation without accessing connected devices.

## Live Serial Monitor (experimental)

The `feature/live-monitor` branch adds a desktop console that keeps every configured
port open and continuously captures incoming data, even when no command is running:

```powershell
.\.venv\Scripts\python.exe serial_live_monitor.py
```

Use `--config PATH` to choose another collection config. It uses the existing
ports, baud, timeout, prompt, line-ending, log-folder, and suite settings.
The desktop window uses Python's bundled Tkinter; `pyserial` is the only external
dependency. The existing one-shot collector remains available.

1. Click **Start listening**. No commands are sent automatically, and serial input
   buffers are not cleared. Boot messages and unsolicited data remain in the log.
2. Enter one command or paste a list with one command per line. **Send command list**
   sends each step to every device, with their writes coordinated together.
   **Send sta** and **Send con** are quick actions. **Load configured commands**
   fills the editor from `commands` without sending anything.
3. Each device must return its configured prompt before the next command is sent
   to any device. All configured devices must be connected before sending a list.
   Only one list runs at a time. A timeout/error cancels the remaining commands;
   a timed-out device stays open so late responses can still be captured.
4. Use the device filter to inspect one device or all devices. **Follow latest**
   controls automatic scrolling. The display keeps recent events; the disk log
   preserves the whole session. Echoes, prompts, blank lines, and unexpected output
   are included. `\r` represents carriage returns in the desktop display.
5. Click **Stop & save report**, then **Open saved report**. Closing the window also
   stops the session and saves the report. You can start another session afterward.

Pre/post commands and test-sequence selection are not applied automatically in
this mode. Only commands you explicitly send are transmitted. Commands may change
device settings depending on what you enter. Synchronized writes are coordinated
by software and are not guaranteed to occur at exactly the same physical instant.
The monitor owns its COM ports while listening, so stop other serial tools using
those same ports before connecting.

Each session writes:

```text
logs/live_serial_YYYYMMDD_HHMMSS_microseconds.jsonl
logs/live_serial_YYYYMMDD_HHMMSS_microseconds_viewer.html
```

The JSONL file is appended and flushed after each event, making already-written
records available during capture. Logging errors stop capture and are surfaced in
the window. This is a different event format from the collector JSON and is not
currently imported into the timeline builder. Each record includes a unique
increasing ID, local timestamp with timezone offset, port, and event type:

- `tx` / `rx`: sent/received chunks, readable text, byte count, and exact bytes as
  `bytes_base64`. Command sends also identify the command, batch, and step.
- `command_result`: prompt completion, timeout, error, or cancellation.
- `connection`, `batch_start`, `batch_end`, `session_start`, `session_end`, and
  `error`: session and device activity.

Timestamps reflect when the computer writes or reads data, not the device's
internal event time. Reads may contain partial lines or several lines together.
RX events retain batch/step context while a command is active, but unexpected
messages can arrive during that command too; the log does not assert that all RX
bytes are replies to that command. Completion uses the configured prompt, so a
late prompt or unsolicited prompt-like line cannot be distinguished from a reply
by this protocol alone. Before retrying a timed-out device, wait for it to settle.

If the app is interrupted before creating the HTML report, regenerate it from
the saved JSONL file without connecting to any devices:

```powershell
.\.venv\Scripts\python.exe serial_live_monitor.py --report logs/live_serial_YYYYMMDD_HHMMSS_microseconds.jsonl
```

Run both suites without connecting to hardware:

```powershell
.\.venv\Scripts\python.exe -m unittest -v test_serial_at_test test_live_serial
```

## View Individual Logs

Each collection run saves JSON and, unless `--no-viewer` is used, a matching HTML file:

```text
logs/serial_log_YYYYMMDD_HHMMSS.json
logs/serial_log_YYYYMMDD_HHMMSS_viewer.html
```

Open the `_viewer.html` file to inspect one run. The viewer supports:

- Dark mode.
- Device cards side by side.
- Command filtering.
- Parsed fields and raw cleaned response lines.
- Expand/collapse controls.

You can also open `log_viewer.html` manually and choose any serial JSON file with the file picker.

## Configure Devices

Use `serial_configure.py` when you are changing device settings. This workflow is intentionally separate from log collection because it sends write commands.

```bash
python serial_configure.py --config device_configure.json
```

The script configures each COM port listed in `device_configure.json`, one at a time. It prints the commands it is about to send and requires you to type:

```text
YES
```

To configure only one port instead of the configured list:

```bash
python serial_configure.py --config device_configure.json --port COM12
```

To skip the confirmation prompt:

```bash
python serial_configure.py --config device_configure.json --yes
```

To configure without opening the browser:

```bash
python serial_configure.py --config device_configure.json --no-viewer
```

The script sends a blank line first and waits for the `# SGS` prompt before applying settings. It then runs verification commands and writes:

```text
logs/configure_log_YYYYMMDD_HHMMSS.json
logs/configure_log_YYYYMMDD_HHMMSS_viewer.html
```

Open the `_viewer.html` file to scan the captured apply and verify command responses. The configure viewer does not analyze, select, or confirm fields; it is only for readable review and posterity.

### `device_configure.json`

Important fields:

- `ports`: COM ports to configure one at a time.
- `baud`: serial baud rate.
- `timeout`: seconds to wait for prompt after each command.
- `prompt_prefix`: prompt text that marks command completion.
- `line_ending`: command ending, usually `cr`.
- `log_dir`: output folder for configure logs.
- `apply_commands`: setting-changing commands.
- `verify_commands`: readback commands, usually `con` and `sta`.

## Build Timeline Report

Use `build_timeline.py` to correlate condensed test spreadsheet data with all serial and configure JSON logs. Point it at a folder and it scans recursively.

```bash
python build_timeline.py .
```

The report opens in your browser automatically.

Folder mode looks for:

```text
condensedTestResults.csv, or one CSV file directly in the chosen folder
**/serial_log_*.json
**/configure_log_*.json
```

The generated output filenames are based on the logs' `test_suite` value:

```text
<test_suite>_timeline_index.json
<test_suite>_timeline_report.html
```

If there are multiple CSV files directly in the folder, pass `--csv` so the builder does not guess.
Use `--no-viewer` if you only want to generate the files without opening the browser.

The report shows a scaled timeline across the full test/log time range:

- `T` markers for spreadsheet test timestamps.
- `L` markers for collection log timestamps.
- `C` markers for configure log timestamps.
- Hover quickviews for fast inspection.
- Click-to-expand details in the side panel.
- Device filtering by IMEI.
- Text search.
- `Compress Gaps` and `Actual Time` timeline scale modes.
- Dark mode.
- `Split Detail` and `Wide Timeline` layouts.
- Links from each log marker to its full individual log viewer.

### `condensedTestResults.csv`

Expected columns:

```text
Timestamp,Fault test,Expected Result,<IMEI columns...>,Notes
```

The IMEI columns are treated as per-device test results. Multiline CSV cells are supported.

Custom paths can be passed if needed:

```bash
python build_timeline.py --csv condensedTestResults.csv --logs logs --index timeline_index.json --report timeline_report.html
```

## Typical Workflow

1. Configure devices if needed:

```bash
python serial_configure.py --config device_configure.json
```

2. Collect serial evidence before or after a test step:

```bash
python serial_at_test.py --config collection_config.json
```

3. Update/export `condensedTestResults.csv` from the spreadsheet.

4. Rebuild the timeline:

```bash
python build_timeline.py .
```

## Notes

- JSON logs are the source of truth. HTML reports are regenerated views.
- The collector is intended for read-only evidence commands.
- The configure script is intended for write commands and verification.
- If a command times out, check COM port, wiring, prompt readiness, baud rate, and whether the device returned to the `# SGS` prompt.
