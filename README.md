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

## Collect Serial Logs

Use `serial_at_test.py` to poll one or more devices and save JSON logs.

```bash
python serial_at_test.py --config collection_config.json
```

This reads `collection_config.json`, asks which configured test sequence is being collected, opens all listed COM ports in parallel, sends each command, waits for the `# SGS` prompt, writes a JSON log to `logs/`, generates a matching `_viewer.html`, and opens it in your browser.

To choose a sequence without the prompt:

```bash
python serial_at_test.py --config collection_config.json --sequence "SM Load Fault"
```

To list available sequences:

```bash
python serial_at_test.py --config collection_config.json --list-sequences
```

To collect without opening the browser:

```bash
python serial_at_test.py --config collection_config.json --no-viewer
```

### `collection_config.json`

Important fields:

- `ports`: COM ports to poll at the same time.
- `test_suite`: title shown in generated viewers and logs.
- `test_sequences`: named test sequences you can select when starting a collection run.
- `baud`: serial baud rate.
- `timeout`: seconds to wait for the prompt after each command.
- `prompt_prefix`: prompt text that marks command completion, currently `# SGS`.
- `line_ending`: command ending, usually `cr`.
- `log_dir`: output folder for logs.
- `commands`: commands sent to every port, currently `con`, `sta`, and `eve`.
- `pre_commands` / `post_commands`: optional command lists before or after the main commands.

## View Individual Logs

Each collection run creates:

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

The script sends a blank line first and waits for the `# SGS` prompt before applying settings. It then runs verification commands and writes:

```text
logs/configure_log_YYYYMMDD_HHMMSS.json
```

After verification, the terminal prints a summary for each device:

- `Version`
- `Model Number`
- `High Current Cal Factor 650A`
- `GNSS status`
- `Database designation`

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

Use `build_timeline.py` to correlate condensed test spreadsheet data with all serial JSON logs.

```bash
python build_timeline.py
start timeline_report.html
```

The default inputs are:

```text
condensedTestResults.csv
logs/serial_log_*.json
```

The generated outputs are:

```text
timeline_index.json
timeline_report.html
```

The report shows a scaled timeline across the full test/log time range:

- `T` markers for spreadsheet test timestamps.
- `L` markers for serial log timestamps.
- Hover quickviews for fast inspection.
- Click-to-expand details in the side panel.
- Device filtering by IMEI.
- Text search.
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
python build_timeline.py
start timeline_report.html
```

## Notes

- JSON logs are the source of truth. HTML reports are regenerated views.
- The collector is intended for read-only evidence commands.
- The configure script is intended for write commands and verification.
- If a command times out, check COM port, wiring, prompt readiness, baud rate, and whether the device returned to the `# SGS` prompt.
