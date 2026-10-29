"""Desktop console for live, unfiltered multi-device serial sessions."""

import argparse
import base64
import math
import queue
import threading
import webbrowser
from pathlib import Path

from live_serial import LiveSession, SerialReplyDecoder, create_report
from serial_at_test import DEFAULT_CONFIG_PATH, load_config, load_raw_config


def device_columns(width: int, count: int) -> int:
    """Match the report's 360px cards and prefer complete rows for even counts."""
    columns = min(count, max(1, (width + 10) // 370))
    if count % 2 == 0 and columns < count:
        columns = next(candidate for candidate in range(columns, 0, -1) if count % candidate == 0)
    return max(1, columns)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG_PATH)
    parser.add_argument("--report", type=Path, help="Rebuild an HTML report from an existing live JSONL log and exit.")
    args = parser.parse_args()
    try:
        if args.report:
            print(create_report(args.report.resolve()))
            return 0
        config_path = args.config.resolve()
        config = load_config(load_raw_config(config_path))
    except (OSError, ValueError) as exc:
        print(f"ERROR: {exc}")
        return 1

    # Report generation and backend tests do not require a desktop session.
    import tkinter as tk
    from tkinter import messagebox, ttk
    from tkinter.scrolledtext import ScrolledText

    class MonitorWindow:
        def __init__(self, root):
            self.root = root
            self.session = None
            self.report = None
            self.closing = False
            self.saving = False
            self.decoders = {port: SerialReplyDecoder() for port in config.ports}
            self.notifications = queue.Queue()
            root.title("Live serial monitor")
            root.geometry("1150x820")
            root.minsize(760, 580)
            root.protocol("WM_DELETE_WINDOW", self.close)
            body = ttk.Frame(root, padding=16)
            body.pack(fill="both", expand=True)
            ttk.Label(body, text=config.test_suite, font=("Segoe UI", 17, "bold")).pack(anchor="w")
            ttk.Label(
                body, text=f"{config.baud} baud · {config.timeout:g}s command timeout · {config_path.name}",
            ).pack(anchor="w", pady=(4, 12))
            toolbar = ttk.Frame(body)
            toolbar.pack(fill="x")
            self.connect_button = ttk.Button(toolbar, text="Start listening", command=self.connect)
            self.connect_button.pack(side="left")
            self.stop_button = ttk.Button(toolbar, text="Stop & save report", command=self.stop, state="disabled")
            self.stop_button.pack(side="left", padx=8)
            self.report_button = ttk.Button(toolbar, text="Open saved report", command=self.open_report, state="disabled")
            self.report_button.pack(side="left")
            self.state = tk.StringVar(value="Not connected. Start listening to capture incoming data.")
            ttk.Label(body, textvariable=self.state).pack(anchor="w", pady=8)
            self.port_states = {port: "NOT CONNECTED" for port in config.ports}

            composer = ttk.LabelFrame(body, text="Commands to send to every device", padding=10)
            composer.pack(fill="x", pady=12)
            ttk.Label(
                composer, text="One command per line. Each step waits for every device's prompt before the next step.",
            ).pack(anchor="w")
            self.commands = ScrolledText(composer, height=4, font=("Consolas", 11))
            self.commands.pack(fill="x", pady=8)
            self.commands.insert("1.0", "sta")
            actions = ttk.Frame(composer)
            actions.pack(fill="x")
            self.send_button = ttk.Button(actions, text="Send command list", command=self.send, state="disabled")
            self.send_button.pack(side="left")
            ttk.Button(actions, text="Load configured commands", command=self.load_commands).pack(side="right")
            quick_actions = ttk.Frame(composer)
            quick_actions.pack(fill="x", pady=(8, 0))
            self.send_buttons = [self.send_button]
            for command in ("sta", "con", "eve", "b", "fast"):
                button = ttk.Button(
                    quick_actions, text=f"Send {command}", state="disabled",
                    command=lambda command=command: self.send([command]),
                )
                button.pack(side="left", padx=(0, 8))
                self.send_buttons.append(button)
            ttk.Label(
                composer, text="Only commands you send here are transmitted. Pre/post commands do not run automatically.",
            ).pack(anchor="w", pady=(8, 0))

            display_controls = ttk.Frame(body)
            display_controls.pack(fill="x")
            ttk.Label(display_controls, text="Live device replies").pack(side="left")
            self.follow = tk.BooleanVar(value=True)
            ttk.Checkbutton(display_controls, text="Follow latest", variable=self.follow).pack(side="right")
            self.panes = ttk.Frame(body)
            self.panes.pack(fill="both", expand=True, pady=8)
            self.outputs = {}
            self.device_states = {}
            self.device_frames = []
            self.layout_size = (0, 0)
            self.layout_columns = None
            for port in config.ports:
                pane = ttk.LabelFrame(self.panes, text=port, padding=5)
                self.device_frames.append(pane)
                self.device_states[port] = tk.StringVar(value="NOT CONNECTED")
                ttk.Label(pane, textvariable=self.device_states[port], wraplength=330).pack(anchor="w")
                output = ScrolledText(
                    pane, wrap="none", state="disabled", font=("Consolas", 10),
                    width=1, height=1, background="#111b25", foreground="#e9f0f7",
                    insertbackground="white",
                )
                horizontal = ttk.Scrollbar(pane, orient="horizontal", command=output.xview)
                horizontal.pack(side="bottom", fill="x")
                output.configure(xscrollcommand=horizontal.set)
                output.pack(fill="both", expand=True)
                output.tag_configure("tx", foreground="#80d9ff")
                self.outputs[port] = output
            self.panes.bind("<Configure>", self.reflow_devices)
            ttk.Label(
                body, text="Replies stream continuously. Sent commands show timestamps; the disk log timestamps all traffic.",
            ).pack(anchor="w")
            self.root.after(50, self.poll)

        def reflow_devices(self, event):
            count = len(self.device_frames)
            columns = device_columns(event.width, count)
            if columns == self.layout_columns:
                return
            for column in range(self.layout_size[0]):
                self.panes.columnconfigure(column, weight=0, uniform="")
            for row in range(self.layout_size[1]):
                self.panes.rowconfigure(row, weight=0, uniform="")
            rows = math.ceil(count / columns)
            # Spanning a common grid lets a short final row fill the width too.
            tracks = math.lcm(columns, count % columns or columns)
            for column in range(tracks):
                self.panes.columnconfigure(column, weight=1, uniform="ports")
            for row in range(rows):
                self.panes.rowconfigure(row, weight=1, uniform="ports")
                row_count = min(columns, count - row * columns)
                span = tracks // row_count
                for column in range(row_count):
                    pane = self.device_frames[row * columns + column]
                    pane.grid(
                        row=row, column=column * span, columnspan=span,
                        sticky="nsew", padx=5, pady=5,
                    )
            self.layout_columns = columns
            self.layout_size = (tracks, rows)

        def refresh_ports(self):
            for port, state in self.port_states.items():
                self.device_states[port].set(state)

        def connect(self):
            try:
                self.session = LiveSession(config, config_path)
                self.session.start()
            except Exception as exc:
                messagebox.showerror("Could not start listening", str(exc))
                return
            for port, output in self.outputs.items():
                output.configure(state="normal")
                output.delete("1.0", "end")
                output.configure(state="disabled")
                self.decoders[port] = SerialReplyDecoder()
            self.report = None
            self.report_button.configure(state="disabled")
            self.connect_button.configure(state="disabled")
            self.stop_button.configure(state="normal")
            self.state.set(f"Listening. Full session log: {self.session.log.path}")

        def send(self, commands=None):
            if self.session is None:
                messagebox.showinfo("Start listening first", "Connect the devices before sending commands.")
                return
            if commands is None:
                commands = [line.strip() for line in self.commands.get("1.0", "end").splitlines() if line.strip()]
            try:
                self.session.send(commands)
            except ValueError as exc:
                messagebox.showinfo("Command list not sent", str(exc))

        def load_commands(self):
            self.commands.delete("1.0", "end")
            self.commands.insert("1.0", "\n".join(config.commands))

        def stop(self):
            if self.session is None or self.saving:
                return
            self.saving = True
            self.stop_button.configure(state="disabled")
            self.state.set("Stopping capture and saving the report…")
            session = self.session

            def finish():
                try:
                    self.notifications.put(("saved", session.stop()))
                except Exception as exc:
                    self.notifications.put(("error", str(exc)))

            threading.Thread(target=finish, name="live-save", daemon=True).start()

        def open_report(self):
            if self.report is not None:
                webbrowser.open(self.report.resolve().as_uri())

        def append(self, event):
            port = event.get("port")
            if port not in self.outputs or event["kind"] not in ("tx", "rx"):
                return
            output = self.outputs[port]
            if event["kind"] == "rx":
                text = self.decoders[port].feed(base64.b64decode(event["bytes_base64"]))
            else:
                # A timestamp belongs to the sent command, never to a read chunk.
                prefix = "" if output.get("end-2c", "end-1c") in ("", "\n") else "\n"
                text = f"{prefix}[{event['timestamp']}] >>> {event['command']}\n"
            self.insert_reply(port, text, event["kind"])

        def insert_reply(self, port, text, tag="rx"):
            output = self.outputs[port]
            output.configure(state="normal")
            output.insert("end", text, tag)
            lines = int(output.index("end-1c").split(".")[0])
            if lines > 15000:
                output.delete("1.0", f"{lines - 12000}.0")
            output.configure(state="disabled")
            if self.follow.get():
                output.see("end")

        def poll(self):
            if self.session is not None:
                for _ in range(500):
                    try:
                        event = self.session.log.events.get_nowait()
                    except queue.Empty:
                        break
                    self.append(event)
                    if event["kind"] == "connection":
                        if event["status"] == "DISCONNECTED":
                            self.insert_reply(event["port"], self.decoders[event["port"]].feed(b"", final=True))
                        self.port_states[event["port"]] = event["status"]
                        self.refresh_ports()
                    if event["kind"] == "command_result":
                        self.device_states[event["port"]].set(
                            f"{self.port_states[event['port']]} | {event['command']}: {event['status']}"
                        )
                    if event["kind"] == "error" and event.get("port"):
                        self.device_states[event["port"]].set(f"ERROR: {event['message']}")
                    if event["kind"] == "error" and not event.get("port"):
                        self.state.set(event["message"])
                ready = (
                    not self.saving and not self.session.stopping.is_set() and not self.session.busy
                    and all(worker.connected.is_set() for worker in self.session.workers.values())
                )
            else:
                ready = False
            for button in self.send_buttons:
                button.configure(state="normal" if ready else "disabled")
            try:
                kind, value = self.notifications.get_nowait()
            except queue.Empty:
                pass
            else:
                self.saving = False
                self.connect_button.configure(state="normal")
                if kind == "saved":
                    self.report = value
                    self.report_button.configure(state="normal")
                    self.state.set(f"Session saved. Report: {value}")
                else:
                    self.state.set(f"Save failed: {value}. Captured JSONL may still be recoverable.")
                    messagebox.showerror("Could not save report", value)
                if self.closing:
                    self.root.destroy()
                    return
            self.root.after(50, self.poll)

        def close(self):
            if self.saving:
                self.closing = True
            elif self.session is not None and not self.session.closed:
                self.closing = True
                self.stop()
            else:
                self.root.destroy()

    root = tk.Tk()
    MonitorWindow(root)
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
