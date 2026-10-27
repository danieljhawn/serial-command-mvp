"""Desktop console for live, unfiltered multi-device serial sessions."""

import argparse
from collections import deque
import queue
import threading
import webbrowser
from pathlib import Path

from live_serial import LiveSession, create_report, event_text
from serial_at_test import DEFAULT_CONFIG_PATH, load_config, load_raw_config


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
            self.history = deque(maxlen=5000)
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
            self.device_state = tk.StringVar()
            ttk.Label(body, textvariable=self.device_state, wraplength=1050).pack(anchor="w")
            self.refresh_ports()

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
            self.send_button = ttk.Button(actions, text="Send command list", command=self.send)
            self.send_button.pack(side="left")
            self.sta_button = ttk.Button(actions, text="Send sta", command=lambda: self.send(["sta"]))
            self.sta_button.pack(side="left", padx=8)
            self.con_button = ttk.Button(actions, text="Send con", command=lambda: self.send(["con"]))
            self.con_button.pack(side="left")
            ttk.Button(actions, text="Load configured commands", command=self.load_commands).pack(side="right")
            ttk.Label(
                composer, text="Only commands you send here are transmitted. Pre/post commands do not run automatically.",
            ).pack(anchor="w", pady=(8, 0))

            filters = ttk.Frame(body)
            filters.pack(fill="x")
            ttk.Label(filters, text="Show device:").pack(side="left")
            self.port_filter = tk.StringVar(value="All devices")
            selector = ttk.Combobox(
                filters, textvariable=self.port_filter,
                values=["All devices", *config.ports], state="readonly", width=18,
            )
            selector.pack(side="left", padx=8)
            selector.bind("<<ComboboxSelected>>", self.redraw)
            self.follow = tk.BooleanVar(value=True)
            ttk.Checkbutton(filters, text="Follow latest", variable=self.follow).pack(side="right")
            self.output = ScrolledText(
                body, wrap="word", state="disabled", font=("Consolas", 10),
                background="#111b25", foreground="#e9f0f7", insertbackground="white",
            )
            self.output.pack(fill="both", expand=True, pady=8)
            for tag, color in (("tx", "#80d9ff"), ("rx", "#a6e2a0"), ("error", "#ffad9f")):
                self.output.tag_configure(tag, foreground=color)
            ttk.Label(
                body, text="RX stays active between commands. Display keeps recent events; the disk log keeps the full session.",
            ).pack(anchor="w")
            self.root.after(50, self.poll)

        def refresh_ports(self):
            self.device_state.set("   |   ".join(f"{port}: {state}" for port, state in self.port_states.items()))

        def connect(self):
            try:
                self.session = LiveSession(config, config_path)
                self.session.start()
            except Exception as exc:
                messagebox.showerror("Could not start listening", str(exc))
                return
            self.history.clear()
            self.redraw()
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
            if self.port_filter.get() not in ("All devices", event.get("port")) and event.get("port"):
                return
            content = event_text(event).replace("\r", "\\r")
            text = f"{event['timestamp']}  {event.get('port') or 'SESSION'}  {event['kind'].upper()}\n{content}\n\n"
            self.output.configure(state="normal")
            self.output.insert("end", text, event["kind"])
            lines = int(self.output.index("end-1c").split(".")[0])
            if lines > 15000:
                self.output.delete("1.0", f"{lines - 12000}.0")
            self.output.configure(state="disabled")
            if self.follow.get():
                self.output.see("end")

        def redraw(self, event=None):
            self.output.configure(state="normal")
            self.output.delete("1.0", "end")
            self.output.configure(state="disabled")
            for record in self.history:
                self.append(record)

        def poll(self):
            if self.session is not None:
                for _ in range(500):
                    try:
                        event = self.session.log.events.get_nowait()
                    except queue.Empty:
                        break
                    self.history.append(event)
                    self.append(event)
                    if event["kind"] == "connection":
                        self.port_states[event["port"]] = event["status"]
                        self.refresh_ports()
                    if event["kind"] == "error" and not event.get("port"):
                        self.state.set(event["message"])
                ready = (
                    not self.saving and not self.session.stopping.is_set() and not self.session.busy
                    and all(worker.connected.is_set() for worker in self.session.workers.values())
                )
            else:
                ready = False
            for button in (self.send_button, self.sta_button, self.con_button):
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
