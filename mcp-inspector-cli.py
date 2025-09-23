#!/usr/bin/env python3
import json
import os
import re
import subprocess
import sys
import threading
import time
from collections import deque
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

# Rich imports for enhanced terminal output
from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text
from rich.columns import Columns
from rich.progress import Progress, SpinnerColumn, TextColumn, TimeElapsedColumn
from rich.syntax import Syntax
from rich.theme import Theme
from rich.live import Live
from rich.layout import Layout
from rich.align import Align


def _timestamp() -> str:
    return datetime.now().strftime("%H:%M:%S.%f")[:-3]


# Rich theme and console setup for enhanced terminal output
# Adjusted for readability on both dark and light backgrounds
RICH_THEME = Theme({
    "success": "green",
    "error": "red",
    "warning": "dark_orange3",
    "info": "blue",
    "accent": "blue",
    "secondary": "grey30",
    "muted": "grey27",
    "highlight": "bold blue",
    "header": "bold blue",
    "timestamp": "grey30",
    "json": "green",
    "uri": "blue underline",
    "parameter": "magenta",
    "description": "grey23",
    "border": "grey35",
    "table.header": "bold blue",
    "table.border": "grey35",
    "panel.border": "grey35",
    "panel.title": "bold blue",
})

# Ensure our own stdout/stderr can handle UTF-8 gracefully on Windows consoles
try:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    # Best-effort; continue if not supported
    pass

# Global console instance
console = Console(theme=RICH_THEME)

# Legacy ANSI color support for backward compatibility
ANSI_SUPPORTED = os.name != "nt" or os.environ.get("WT_SESSION") or os.environ.get("ANSICON")
RESET = "\x1b[0m" if ANSI_SUPPORTED else ""
GREEN = "\x1b[32m" if ANSI_SUPPORTED else ""
RED = "\x1b[31m" if ANSI_SUPPORTED else ""
YELLOW = "\x1b[33m" if ANSI_SUPPORTED else ""
CYAN = "\x1b[36m" if ANSI_SUPPORTED else ""
BLUE = "\x1b[34m" if ANSI_SUPPORTED else ""
DARK_GRAY = "\x1b[90m" if ANSI_SUPPORTED else ""
MAGENTA = "\x1b[35m" if ANSI_SUPPORTED else ""
WHITE = "\x1b[37m" if ANSI_SUPPORTED else ""

# Readable dark theme colors (legacy)
DIM_GREEN = "\x1b[32m" if ANSI_SUPPORTED else ""
DIM_BLUE = "\x1b[34m" if ANSI_SUPPORTED else ""
DIM_CYAN = "\x1b[36m" if ANSI_SUPPORTED else ""
DIM_WHITE = "\x1b[37m" if ANSI_SUPPORTED else ""
GRAY = "\x1b[90m" if ANSI_SUPPORTED else ""
DARKER_GRAY = "\x1b[2m" if ANSI_SUPPORTED else ""

ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


def _strip_ansi(text: str) -> str:
    return ANSI_RE.sub("", text)


class MCPTester:
    def __init__(self, config_path: Optional[str] = None) -> None:
        self.working_dir = os.path.dirname(os.path.abspath(__file__))
        self.config_path = config_path
        self.session_stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        self.log_path = os.path.join(self.working_dir, f"session-{self.session_stamp}.txt")

        # Rich console for enhanced output
        self.console = Console(theme=RICH_THEME)

        self.process: Optional[subprocess.Popen[str]] = None
        self.id_to_response: Dict[int, Dict[str, Any]] = {}
        self.id_lock = threading.Lock()
        self.id_cv = threading.Condition(self.id_lock)
        self.stdout_buffer: deque[str] = deque(maxlen=1000)
        self.stderr_buffer: deque[str] = deque(maxlen=1000)
        self.next_id: int = 1
        self.last_status_colored: Optional[str] = None
        self.last_result_preview: Optional[str] = None
        self.last_status_time: float = 0.0

        # Progress tracking for long operations
        self.current_progress: Optional[Progress] = None
        self.live_display: Optional[Live] = None

        # server selection support
        self.servers: List[Tuple[str, List[str], str]] = []  # (name, command_with_args, cwd)
        self.selected_index: int = -1
        self.selected_cmd: Optional[List[str]] = None
        self.selected_cwd: Optional[str] = None

    def _build_child_env(self) -> Dict[str, str]:
        """Build a clean environment for the child process with UTF-8 I/O enabled.

        - Removes parent VIRTUAL_ENV to avoid uv warning when targeting another project
        - Forces UTF-8 for Python stdio in the child process
        """
        env = os.environ.copy()
        # Avoid uv warning when child project has its own .venv
        env.pop("VIRTUAL_ENV", None)
        # Force UTF-8 encoding for Python I/O in the child
        env["PYTHONUTF8"] = "1"
        env["PYTHONIOENCODING"] = "utf-8"
        return env

    def start_progress(self, description: str, total: Optional[float] = None) -> None:
        """Start a progress indicator for long-running operations"""
        self.current_progress = Progress(
            SpinnerColumn(),
            TextColumn(f"[bold blue]{description}"),
            TimeElapsedColumn(),
        )
        self.live_display = Live(self.current_progress, console=self.console, refresh_per_second=4)
        self.live_display.start()

    def stop_progress(self) -> None:
        """Stop the current progress indicator"""
        if self.live_display:
            self.live_display.stop()
            self.live_display = None
            self.current_progress = None

    # ---------- logging ----------
    def log_line(self, line: str) -> None:
        try:
            with open(self.log_path, "a", encoding="utf-8") as f:
                f.write(_strip_ansi(line) + "\n")
        except Exception:
            pass

    @staticmethod
    def _trim_before_doc_sections(text: str) -> str:
        """Return text up to the first doc section heading like Args/Parameters/Returns.

        This keeps the human-readable summary while hiding verbose sections
        that duplicate information shown elsewhere in the UI.
        """
        if not text:
            return text
        match = re.search(r"\n\s*(Args?|Arguments?|Parameters?|Returns?|Examples?|Usage|Notes?)\s*:\s*",
                          text, flags=re.IGNORECASE)
        if match:
            return text[: match.start()].strip()
        return text.strip()

    def print_and_log(self, line: str) -> None:
        print(line)
        self.log_line(line)

    def colored_print(self, line: str, color: str, end: str = "\n") -> None:
        """Print with color (for display only, not logged)"""
        print(f"{color}{line}{RESET}", end=end)

    def display_tools_list(self, tools: List[Dict[str, Any]]) -> None:
        """Display tools in an enhanced table format using Rich"""
        # Create server info text
        if self.selected_index >= 0 and self.selected_index < len(self.servers):
            server_name, _, _ = self.servers[self.selected_index]
            title = f"Tools from \"{server_name}\""
        else:
            title = "Available Tools"

        if not tools:
            panel = Panel(
                "[warning]No tools available.[/warning]",
                title=title,
                border_style="panel.border",
                title_align="left"
            )
            self.console.print(panel)
            return

        # Create table for tools
        table = Table(
            title=title,
            title_style="panel.title",
            header_style="table.header",
            border_style="table.border",
            show_lines=True
        )

        table.add_column("#", style="bold", justify="right", width=3)
        table.add_column("Tool Name", style="bold", width=25)
        table.add_column("Description", style="description", width=50, overflow="fold")
        table.add_column("Parameters", style="parameter", width=20)

        for idx, tool in enumerate(tools):
            name = tool.get('name', 'Unknown')
            description = self._trim_before_doc_sections(tool.get('description', '').strip())

            # Get parameter count from inputSchema
            input_schema = tool.get('inputSchema', {})
            properties = input_schema.get('properties', {})
            param_count = len(properties) if properties else 0
            param_info = f"{param_count} params" if param_count > 0 else "None"

            # Show full description (wrapped in cell)

            table.add_row(
                str(idx),
                name,
                description or "[dim]No description[/dim]",
                param_info
            )

        self.console.print(table)

    def display_tool_details(self, tool: Dict[str, Any]) -> None:
        """Display detailed information about a specific tool using Rich panels"""
        name = tool.get('name', 'Unknown')
        description = tool.get('description', '').strip()
        input_schema = tool.get('inputSchema', {})

        # Main tool info panel
        content_lines = []
        if description:
            content_lines.append(f"[bold green]Description:[/bold green]")
            content_lines.append(f"{description}")
            content_lines.append("")  # Empty line

        # Display parameter information if available
        properties = input_schema.get('properties', {})
        required = input_schema.get('required', [])

        if properties:
            content_lines.append("[bold green]Parameters:[/bold green]")
            for param_name, param_info in properties.items():
                param_type = param_info.get('type', 'any')
                param_desc = param_info.get('description', '')
                default_val = param_info.get('default')
                is_required = param_name in required

                status_parts = []
                if is_required:
                    status_parts.append("[red]required[/red]")
                else:
                    status_parts.append("[yellow]optional[/yellow]")
                if default_val is not None:
                    status_parts.append(f"default: [cyan]{default_val}[/cyan]")

                status = ", ".join(status_parts)

                content_lines.append(f"  • [bold]{param_name}[/bold] ([blue]{param_type}[/blue]) - {status}")
                if param_desc:
                    content_lines.append(f"    [dim]{param_desc}[/dim]")
        else:
            content_lines.append("[bold green]Parameters:[/bold green] None")

        content = "\n".join(content_lines)

        panel = Panel(
            content,
            title=f"Tool Details: {name}",
            title_align="left",
            border_style="panel.border",
            padding=(1, 2)
        )

        self.console.print(panel)

    def display_resources_list(self, resources: List[Dict[str, Any]]) -> None:
        """Display resources in an enhanced table format using Rich"""
        # Create server info text
        if self.selected_index >= 0 and self.selected_index < len(self.servers):
            server_name, _, _ = self.servers[self.selected_index]
            title = f"Resources from \"{server_name}\""
        else:
            title = "Available Resources"

        if not resources:
            panel = Panel(
                "[warning]No resources available.[/warning]",
                title=title,
                border_style="panel.border",
                title_align="left"
            )
            self.console.print(panel)
            return

        # Create table for resources
        table = Table(
            title=title,
            title_style="panel.title",
            header_style="table.header",
            border_style="table.border",
            show_lines=True
        )

        table.add_column("#", style="bold", justify="right", width=3)
        table.add_column("URI", style="uri", width=40)
        table.add_column("Name", style="bold", width=20)
        table.add_column("Type", style="parameter", width=15)
        table.add_column("Description", style="description", width=50, overflow="fold")

        for idx, resource in enumerate(resources):
            uri = resource.get('uri', 'Unknown')
            name = resource.get('name', '')
            description = self._trim_before_doc_sections(resource.get('description', '').strip())
            mime_type = resource.get('mimeType', '')

            # Format name - show URI if no name or if they're the same
            display_name = name if name and name != uri else ""

            # Truncate URI if too long
            display_uri = uri
            if len(display_uri) > 37:
                display_uri = display_uri[:34] + "..."

            # Show full description (wrapped in cell)

            table.add_row(
                str(idx),
                display_uri,
                display_name or "[dim]—[/dim]",
                mime_type or "[dim]—[/dim]",
                description or "[dim]No description[/dim]"
            )

        self.console.print(table)

    def display_prompts_list(self, prompts: List[Dict[str, Any]]) -> None:
        """Display prompts in an enhanced table format using Rich"""
        # Create server info text
        if self.selected_index >= 0 and self.selected_index < len(self.servers):
            server_name, _, _ = self.servers[self.selected_index]
            title = f"Prompts from \"{server_name}\""
        else:
            title = "Available Prompts"

        if not prompts:
            panel = Panel(
                "[warning]No prompts available.[/warning]",
                title=title,
                border_style="panel.border",
                title_align="left"
            )
            self.console.print(panel)
            return

        # Create table for prompts
        table = Table(
            title=title,
            title_style="panel.title",
            header_style="table.header",
            border_style="table.border",
            show_lines=True
        )

        table.add_column("#", style="bold", justify="right", width=3)
        table.add_column("Prompt Name", style="bold", width=25)
        table.add_column("Arguments", style="parameter", width=25)
        table.add_column("Description", style="description", width=60, overflow="fold")

        for idx, prompt in enumerate(prompts):
            name = prompt.get('name', 'Unknown')
            description = self._trim_before_doc_sections(prompt.get('description', '').strip())
            arguments = prompt.get('arguments', {})

            # Format arguments info
            arg_info = "[dim]None[/dim]"
            if arguments and isinstance(arguments, dict):
                props = arguments.get('properties', {})
                if props:
                    arg_names = list(props.keys())
                    arg_info = f"[yellow]{', '.join(arg_names)}[/yellow]"

            # Show full description (wrapped in cell)

            table.add_row(
                str(idx),
                name,
                arg_info,
                description or "[dim]No description[/dim]"
            )

        self.console.print(table)

    def summary(self, title: str, attempted: str, response: Optional[Dict[str, Any]]) -> None:
        success = response is not None and "error" not in response

        # Create Rich status display
        timestamp = _timestamp()
        status_icon = "✅" if success else "❌"
        status_style = "success" if success else "error"
        status_text = "SUCCESS" if success else "ERROR"

        # Create a panel for the summary
        summary_content = f"[{timestamp}] {title}: {attempted}"
        panel = Panel(
            f"[{status_style}]{status_text}[/{status_style}]\n{summary_content}",
            title="Operation Summary",
            title_align="left",
            border_style="border",
            padding=(0, 1)
        )

        self.console.print(panel)

        # Log the plain text version
        msg = f"[{timestamp}] {title}: {attempted} -> {status_text}"
        self.log_line(msg)
        self.last_status_colored = msg
        self.last_status_time = time.time()

        if not success:
            if response is None:
                errline = "No response received. The server may have exited or timed out."
            else:
                err = response.get("error", {})
                errline = f"Error: code={err.get('code')} message={err.get('message')} data={err.get('data')}"

            # Display error in a warning panel
            error_panel = Panel(
                f"[error]{errline}[/error]",
                title="Error Details",
                title_align="left",
                border_style="red",
                padding=(0, 1)
            )
            self.console.print(error_panel)

            self.log_line(errline)
            # Include detail in last preview, too
            self.last_result_preview = errline

    # ---------- config ----------
    def load_config_path(self) -> Optional[str]:
        if self.config_path:
            return self.config_path
        if len(sys.argv) >= 2 and sys.argv[1].strip():
            return sys.argv[1]
        local = os.path.join(self.working_dir, "mcp.json")
        if os.path.exists(local):
            return local
        home_cursor = os.path.join(os.path.expanduser("~"), ".cursor", "mcp.json")
        if os.path.exists(home_cursor):
            return home_cursor
        return None

    def load_servers(self) -> None:
        """Populate self.servers from config. Supports Cursor-style mcp.json with multiple servers."""
        cfg_path = self.load_config_path()
        self.servers = []
        if cfg_path and os.path.exists(cfg_path):
            try:
                with open(cfg_path, "r", encoding="utf-8") as f:
                    cfg = json.load(f)
                self.print_and_log(f"[{_timestamp()}] Loaded config from {cfg_path}")
                if "mcpServers" in cfg and isinstance(cfg["mcpServers"], dict):
                    for name, server in cfg["mcpServers"].items():
                        command = server.get("command")
                        if not command:
                            continue
                        args = server.get("args", [])
                        cmd = [command] + list(args)
                        cwd_from_args = None
                        for i, token in enumerate(args):
                            if token == "--directory" and i + 1 < len(args):
                                cwd_from_args = args[i + 1]
                                break
                        self.servers.append((name, cmd, cwd_from_args or self.working_dir))
                elif "command" in cfg:
                    name = cfg.get("name") or "default"
                    args = cfg.get("args", [])
                    cmd = [cfg["command"]] + list(args)
                    cwd_from_args = None
                    for i, token in enumerate(args):
                        if token == "--directory" and i + 1 < len(args):
                            cwd_from_args = args[i + 1]
                            break
                    self.servers.append((name, cmd, cwd_from_args or self.working_dir))
            except Exception as e:
                self.print_and_log(f"[{_timestamp()}] Failed to read config: {e}")
        if not self.servers:
            # Fallback single server
            self.servers.append(("fallback", ["uv", "run", "main.py"], self.working_dir))

    def choose_server_menu(self) -> bool:
        """Let the user choose a server when multiple are configured. Returns True if selected."""
        if not self.servers:
            self.load_servers()
        if len(self.servers) == 1:
            self.selected_index = 0
            name, cmd, cwd = self.servers[0]
            self.selected_cmd, self.selected_cwd = cmd, cwd
            self.print_and_log(f"[{_timestamp()}] Using server: {name} -> {cmd} (cwd={cwd})")
            return True
        # Interactive selection
        print("Available MCP servers:")
        for idx, (name, cmd, cwd) in enumerate(self.servers):
            bold_name = f"\x1b[1m{name}\x1b[0m" if ANSI_SUPPORTED else name
            print(f"{idx}: {bold_name} -> {cmd} (cwd={cwd})")
        sel = input("Select server index (or blank to cancel): ").strip()
        if not sel:
            return False
        try:
            index = int(sel)
        except Exception:
            print("Invalid index.")
            return False
        if index < 0 or index >= len(self.servers):
            print("Index out of range.")
            return False
        self.selected_index = index
        name, cmd, cwd = self.servers[index]
        self.selected_cmd, self.selected_cwd = cmd, cwd
        self.print_and_log(f"[{_timestamp()}] Selected server: {name} -> {cmd} (cwd={cwd})")
        return True

    # ---------- process & I/O ----------
    def start(self) -> None:
        if not self.selected_cmd or not self.selected_cwd:
            # Load and choose default if not already selected
            self.load_servers()
            if not self.choose_server_menu():
                raise RuntimeError("No server selected")
        command, cwd = self.selected_cmd, self.selected_cwd
        try:
            self.process = subprocess.Popen(
                command,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                cwd=cwd,
                env=self._build_child_env(),
            )
        except FileNotFoundError:
            # If multiple servers are configured, don't silently fall back; let user pick another
            if len(self.servers) > 1:
                self.print_and_log(f"[{_timestamp()}] Command not found: {command[0]}. Please choose another server.")
                # Clear selection and prompt again
                self.selected_cmd = None
                self.selected_cwd = None
                if not self.choose_server_menu():
                    raise RuntimeError("No server selected")
                return self.start()
            # Single-server case: fall back to python main.py in working dir
            fallback_cmd = [sys.executable or "python", "main.py"]
            self.print_and_log(
                f"[{_timestamp()}] Command not found: {command[0]}; falling back to: {fallback_cmd}"
            )
            self.process = subprocess.Popen(
                fallback_cmd,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                cwd=self.working_dir,
                env=self._build_child_env(),
            )

        threading.Thread(target=self._stdout_reader, daemon=True).start()
        threading.Thread(target=self._stderr_reader, daemon=True).start()

    def _stdout_reader(self) -> None:
        assert self.process and self.process.stdout
        server_name = "unknown"
        if self.selected_index >= 0 and self.selected_index < len(self.servers):
            server_name, _, _ = self.servers[self.selected_index]

        for raw_line in self.process.stdout:
            line = raw_line.strip()
            if not line:
                continue

            # Enhanced logging format for MCP server messages in blue
            timestamp = _timestamp()
            mcp_msg = f"{BLUE}MCP-{server_name}:{timestamp} {line}{RESET}"
            print(mcp_msg)
            self.log_line(_strip_ansi(mcp_msg))

            self.stdout_buffer.append(line)
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            msg_id = obj.get("id")
            if msg_id is not None and ("result" in obj or "error" in obj):
                with self.id_cv:
                    self.id_to_response[msg_id] = obj
                    self.id_cv.notify_all()

    def _stderr_reader(self) -> None:
        assert self.process and self.process.stderr
        for raw_line in self.process.stderr:
            line = raw_line.rstrip("\n")
            if line:
                # Color stderr messages in dark gray
                timestamp = _timestamp()
                colored_msg = f"{DARK_GRAY}[{timestamp}] ! STDERR: {line}{RESET}"
                print(colored_msg)
                self.log_line(_strip_ansi(colored_msg))
                self.stderr_buffer.append(line)

    def send_notification(self, notification: Dict[str, Any]) -> None:
        payload = json.dumps(notification)
        server_name = "unknown"
        if self.selected_index >= 0 and self.selected_index < len(self.servers):
            server_name, _, _ = self.servers[self.selected_index]

        # Enhanced logging format for notifications
        timestamp = _timestamp()
        inspector_msg = f"INSPECTOR:{timestamp} NOTIFICATION: {json.dumps(notification, indent=2)}"
        print(inspector_msg)
        self.log_line(inspector_msg)

        assert self.process and self.process.stdin
        self.process.stdin.write(payload + "\n")
        self.process.stdin.flush()

    def send_request(self, request: Dict[str, Any], timeout: float = 10.0, interactive_extend: bool = False) -> Optional[Dict[str, Any]]:
        payload = json.dumps(request)
        server_name = "unknown"
        if self.selected_index >= 0 and self.selected_index < len(self.servers):
            server_name, _, _ = self.servers[self.selected_index]

        # Enhanced logging format with Rich syntax highlighting
        timestamp = _timestamp()
        inspector_msg = f"INSPECTOR:{timestamp} {json.dumps(request, indent=2)}"

        # Display request with Rich syntax highlighting
        json_syntax = Syntax(json.dumps(request, indent=2), "json", theme="monokai", line_numbers=False)
        request_panel = Panel(
            json_syntax,
            title=f"Request [{timestamp}]",
            title_align="left",
            border_style="cyan",
            padding=(1, 2)
        )
        self.console.print(request_panel)

        self.log_line(inspector_msg)

        msg_id = request.get("id")
        assert self.process and self.process.stdin
        with self.id_lock:
            if msg_id in self.id_to_response:
                del self.id_to_response[msg_id]
        self.process.stdin.write(payload + "\n")
        self.process.stdin.flush()

        if msg_id is None:
            return None

        start = time.time()
        with self.id_cv:
            while True:
                if msg_id in self.id_to_response:
                    resp = self.id_to_response.pop(msg_id)
                    # Enhanced logging format for responses with Rich syntax highlighting
                    timestamp = _timestamp()
                    mcp_msg = f"MCP-{server_name}:{timestamp} {json.dumps(resp, indent=2)}"

                    # Display response with Rich syntax highlighting
                    json_syntax = Syntax(json.dumps(resp, indent=2), "json", theme="monokai", line_numbers=False)
                    response_panel = Panel(
                        json_syntax,
                        title=f"Response from {server_name} [{timestamp}]",
                        title_align="left",
                        border_style="green",
                        padding=(1, 2)
                    )
                    self.console.print(response_panel)

                    self.log_line(mcp_msg)
                    return resp
                remaining = timeout - (time.time() - start)
                if remaining <= 0:
                    if not interactive_extend:
                        self.console.print(f"[error]✖ Timeout waiting for response id={msg_id}[/error]")
                        self.log_line(f"[{_timestamp()}] ✖ Timeout waiting for response id={msg_id}")
                        return None

                    # Ask user to extend wait without resending - use Rich prompt
                    method = request.get("method", "?")
                    self.start_progress(f"Waiting for {method} (id={msg_id})")

                    try:
                        # Create a prompt panel
                        prompt_panel = Panel(
                            f"Timed out waiting for [bold]{method}[/bold] (id={msg_id}).\n"
                            "Wait 30 seconds more?",
                            title="Timeout Decision",
                            border_style="yellow",
                            padding=(1, 2)
                        )
                        self.console.print(prompt_panel)

                        choice = input("Choice [Y/n]: ").strip().lower()
                    except EOFError:
                        choice = "y"
                    finally:
                        self.stop_progress()

                    if choice in ("", "y", "yes"):
                        self.console.print(f"[info]Extending wait by 30s for id={msg_id}[/info]")
                        self.log_line(f"[{_timestamp()}] Extending wait by 30s for id={msg_id}")
                        # Reset timer and continue waiting
                        start = time.time()
                        timeout = 30.0
                        continue
                    else:
                        self.console.print(f"[warning]User chose to stop waiting for id={msg_id}[/warning]")
                        self.log_line(f"[{_timestamp()}] User chose to stop waiting for id={msg_id}")
                        return None
                self.id_cv.wait(timeout=remaining)

    # ---------- protocol helpers ----------
    def next_request_id(self) -> int:
        rid = self.next_id
        self.next_id += 1
        return rid

    def initialize(self) -> Optional[Dict[str, Any]]:
        req = {
            "jsonrpc": "2.0",
            "id": self.next_request_id(),
            "method": "initialize",
            "params": {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "mcp_tester", "version": "0.1.0"},
            },
        }
        resp = self.send_request(req, timeout=15.0, interactive_extend=True)
        self.summary("Initialize", "Send initialize with clientInfo", resp)
        return resp

    def send_initialized(self) -> None:
        note = {"jsonrpc": "2.0", "method": "notifications/initialized"}
        self.send_notification(note)
        self.summary("Initialized", "Send notifications/initialized", {"result": {"ok": True}})

    # ---------- feature flows ----------
    def list_tools(self) -> Tuple[Optional[Dict[str, Any]], List[Dict[str, Any]]]:
        req = {"jsonrpc": "2.0", "id": self.next_request_id(), "method": "tools/list"}
        resp = self.send_request(req)
        self.summary("Tools/List", "Request available tools", resp)
        tools: List[Dict[str, Any]] = []
        try:
            if resp and "result" in resp:
                tools = resp["result"].get("tools", []) or []
        except Exception:
            tools = []
        return resp, tools

    def call_tool(self, name: str, arguments: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        req = {
            "jsonrpc": "2.0",
            "id": self.next_request_id(),
            "method": "tools/call",
            "params": {"name": name, "arguments": arguments},
        }
        resp = self.send_request(req, timeout=60.0)
        self.summary("Tools/Call", f"Call {name}", resp)
        # Capture a concise preview for menu recap
        preview = None
        if resp and "result" in resp and isinstance(resp["result"], dict):
            content = resp["result"].get("content")
            if isinstance(content, list) and content:
                first = content[0]
                preview = first.get("text") if isinstance(first, dict) else str(first)
        if preview:
            if len(preview) > 200:
                preview = preview[:200] + "…"
            self.last_result_preview = f"Result preview: {preview}"
        else:
            self.last_result_preview = None
        return resp

    def list_resources(self) -> Tuple[Optional[Dict[str, Any]], List[Dict[str, Any]]]:
        req = {"jsonrpc": "2.0", "id": self.next_request_id(), "method": "resources/list"}
        resp = self.send_request(req)
        self.summary("Resources/List", "Request available resources", resp)
        items: List[Dict[str, Any]] = []
        try:
            if resp and "result" in resp:
                items = resp["result"].get("resources", []) or []
        except Exception:
            items = []
        return resp, items

    def read_resource(self, uri: str) -> Optional[Dict[str, Any]]:
        req = {
            "jsonrpc": "2.0",
            "id": self.next_request_id(),
            "method": "resources/read",
            "params": {"uri": uri},
        }
        resp = self.send_request(req, timeout=60.0)
        self.summary("Resources/Read", f"Read {uri}", resp)
        if resp and "result" in resp:
            self.last_result_preview = f"Read {uri}: ok"
        else:
            self.last_result_preview = None
        return resp

    def list_prompts(self) -> Tuple[Optional[Dict[str, Any]], List[Dict[str, Any]]]:
        req = {"jsonrpc": "2.0", "id": self.next_request_id(), "method": "prompts/list"}
        resp = self.send_request(req)
        self.summary("Prompts/List", "Request available prompts", resp)
        items: List[Dict[str, Any]] = []
        try:
            if resp and "result" in resp:
                items = resp["result"].get("prompts", []) or []
        except Exception:
            items = []
        return resp, items

    def get_prompt(self, name: str, arguments: Optional[Dict[str, Any]] = None) -> Optional[Dict[str, Any]]:
        params: Dict[str, Any] = {"name": name}
        if arguments:
            params["arguments"] = arguments
        req = {
            "jsonrpc": "2.0",
            "id": self.next_request_id(),
            "method": "prompts/get",
            "params": params,
        }
        resp = self.send_request(req, timeout=60.0)
        self.summary("Prompts/Get", f"Get {name}", resp)
        if resp and "result" in resp:
            self.last_result_preview = f"Prompt {name}: ok"
        else:
            self.last_result_preview = None
        return resp

    # ---------- interactive helpers ----------
    @staticmethod
    def _infer_default(prop_schema: Dict[str, Any]) -> Any:
        if "default" in prop_schema:
            return prop_schema["default"]
        t = prop_schema.get("type")
        if t == "integer":
            return 0
        if t == "number":
            return 0
        if t == "boolean":
            return False
        if t == "array":
            return []
        if t == "object":
            return {}
        return ""

    @staticmethod
    def _coerce_value(text: str, prop_schema: Dict[str, Any]) -> Any:
        # If user entered valid JSON, use it directly
        try:
            return json.loads(text)
        except Exception:
            pass
        t = prop_schema.get("type")
        if t == "integer":
            try:
                return int(text)
            except Exception:
                return 0
        if t == "number":
            try:
                return float(text)
            except Exception:
                return 0.0
        if t == "boolean":
            return text.strip().lower() in ("1", "true", "yes", "y")
        return text

    def prompt_for_args_from_schema(self, input_schema: Dict[str, Any]) -> Dict[str, Any]:
        properties: Dict[str, Any] = input_schema.get("properties", {}) or {}
        required: List[str] = input_schema.get("required", []) or []
        args: Dict[str, Any] = {}

        # If no properties at all, don't prompt and use empty args
        if not properties:
            self.print_and_log(f"[{_timestamp()}] Tool has no parameters - calling with empty arguments")
            return {}

        # If properties exist but no required parameters, offer to skip or provide values
        if properties and not required:
            # Show available optional parameters
            param_names = []
            for name, prop in properties.items():
                title = prop.get("title") or name
                param_type = prop.get("type", "any")
                default_val = prop.get("default", "none")
                param_names.append(f"{title} ({param_type}, default: {default_val})")

            params_list = ", ".join(param_names)
            self.colored_print(f"Optional parameters: {params_list}", CYAN)
            choice = input("Enter values for optional parameters? [y/N]: ").strip().lower()
            if choice not in ('y', 'yes'):
                self.print_and_log(f"[{_timestamp()}] Using default values for optional parameters")
                # Use default values for optional parameters
                for name, prop in properties.items():
                    if "default" in prop:
                        args[name] = prop["default"]
                    else:
                        args[name] = self._infer_default(prop)
                return args
        for name, prop in properties.items():
            title = prop.get("title") or name
            t = prop.get("type")
            is_required = name in required
            default_val = self._infer_default(prop)
            hint = f"type={t}" if t else ""
            if not is_required:
                hint += f" default={json.dumps(default_val)}"
            prompt = f"Enter value for {title} ({hint}) {'[required]' if is_required else '[optional]'}: "
            text = input(prompt).strip()
            if not text:
                if is_required:
                    args[name] = default_val
                else:
                    # optional with blank -> skip, unless default explicitly present
                    if "default" in prop:
                        args[name] = default_val
                continue
            args[name] = self._coerce_value(text, prop)
        return args

    # ---------- main menu ----------
    def run_menu(self) -> None:
        while True:
            print()
            # Recap of last operation status + preview
            if self.last_status_colored:
                # Avoid immediate duplicate right after an operation
                if time.time() - self.last_status_time > 0.75:
                    print(self.last_status_colored)
                    self.log_line(_strip_ansi(self.last_status_colored))
            if self.last_result_preview:
                self.print_and_log(self.last_result_preview)
            # Create enhanced menu with Rich layout (numbered, no emojis)
            menu_items = [
                ("1", "List and call tools", "t"),
                ("2", "List and read resources", "r"),
                ("3", "List and get prompts", "p"),
                ("4", "Show recent stdout/stderr", "o"),
                ("5", "Show session log path", "l"),
                ("6", "Show color legend", "c"),
                ("7", "Switch server", "s"),
                ("8", "Quit", "q"),
            ]

            # Create menu table
            menu_table = Table(show_header=False, box=None, padding=(0, 2))
            menu_table.add_column("#", style="bold", width=3, justify="right")
            menu_table.add_column("Action", style="bold")
            menu_table.add_column("Key", style="secondary", width=8)

            for num, action, key in menu_items:
                menu_table.add_row(num, action, f"[{key}]")

            # Create server info panel
            server_info = ""
            if self.selected_index >= 0 and self.selected_index < len(self.servers):
                server_name, _, server_cwd = self.servers[self.selected_index]
                server_info = f"[bold green]Connected to:[/bold green] {server_name}\n[dim]Working directory:[/dim] {server_cwd}"

            # Main menu panel
            menu_panel = Panel(
                Align.center(menu_table),
                title="MCP Tester Menu",
                title_align="center",
                border_style="panel.border",
                padding=(1, 3)
            )

            self.console.print(menu_panel)

            # Server info panel
            if server_info:
                server_panel = Panel(
                    server_info,
                    title="Server Connection",
                    title_align="left",
                    border_style="green",
                    padding=(1, 2)
                )
                self.console.print(server_panel)
            choice_raw = input("> ").strip().lower()
            # Accept numeric shortcuts and letter keys
            numeric_map = {
                "1": "t", "2": "r", "3": "p", "4": "o",
                "5": "l", "6": "c", "7": "s", "8": "q",
            }
            choice = numeric_map.get(choice_raw, choice_raw)
            self.log_line(f"[USER] menu choice: {choice or 'enter'}")

            if choice == "t":
                # Submenu loop to keep using tools without returning to main menu
                tools_cache: Optional[List[Dict[str, Any]]] = None
                while True:
                    if tools_cache is None:
                        _, tools_cache = self.list_tools()
                    tools = tools_cache or []
                    if not tools:
                        self.colored_print("No tools available or tools/list failed.", RED)
                        break

                    # Display tools in human-readable format
                    self.display_tools_list(tools)

                    # Display selection options
                    print("[r] refresh    [b] back to main    [q] quit")
                    sel = input("> ").strip().lower()
                    if not sel:
                        # empty -> back
                        break
                    if sel == "b":
                        break
                    if sel == "q":
                        return  # quit the entire menu
                    if sel == "r":
                        tools_cache = None
                        continue
                    try:
                        index = int(sel)
                    except Exception:
                        print("Invalid selection.")
                        continue
                    if index < 0 or index >= len(tools):
                        print("Index out of range.")
                        continue
                    
                    # Show tool details first
                    tool = tools[index]
                    self.display_tool_details(tool)
                    
                    # Ask user what to do next
                    action = input("Action: [c]all tool, [b]ack to list, [q]uit: ").strip().lower()
                    if action == "b" or action == "":
                        continue  # Go back to tool list
                    elif action == "q":
                        return  # Quit entirely
                    elif action == "c":
                        # Call the tool
                        name = tool.get("name")
                        input_schema = tool.get("inputSchema") or {}
                        args = self.prompt_for_args_from_schema(input_schema)
                        self.call_tool(name, args)
                        # After tool call, go back to tool list
                        continue
                    else:
                        print("Invalid action. Returning to tool list.")
                        continue

            elif choice == "r":
                _, resources = self.list_resources()
                if not resources:
                    self.colored_print("No resources available or resources/list failed.", RED)
                    continue

                # Display resources in human-readable format
                self.display_resources_list(resources)

                sel = input("Select resource index to read (or blank to cancel): ").strip()
                if not sel:
                    continue
                try:
                    index = int(sel)
                except Exception:
                    print("Invalid index.")
                    continue
                if index < 0 or index >= len(resources):
                    print("Index out of range.")
                    continue
                uri = resources[index].get("uri")
                if not uri:
                    print("Selected resource has no uri.")
                    continue
                self.read_resource(uri)

            elif choice == "p":
                _, prompts = self.list_prompts()
                if not prompts:
                    self.colored_print("No prompts available or prompts/list failed.", RED)
                    continue

                # Display prompts in human-readable format
                self.display_prompts_list(prompts)

                sel = input("Select prompt index to get (or blank to cancel): ").strip()
                if not sel:
                    continue
                try:
                    index = int(sel)
                except Exception:
                    print("Invalid index.")
                    continue
                if index < 0 or index >= len(prompts):
                    print("Index out of range.")
                    continue
                prompt = prompts[index]
                name = prompt.get("name")
                # Optional: attempt to read arguments schema if present
                args_schema = prompt.get("arguments", {}).get("properties") if isinstance(prompt.get("arguments"), dict) else None
                args: Optional[Dict[str, Any]] = None
                if args_schema:
                    args = self.prompt_for_args_from_schema({"properties": args_schema})
                else:
                    raw = input("Enter JSON for prompt arguments (or blank for none): ").strip()
                    if raw:
                        try:
                            args = json.loads(raw)
                        except Exception:
                            print("Invalid JSON; ignoring arguments.")
                            args = None
                self.get_prompt(name, args)

            elif choice == "o":
                print(f"[{_timestamp()}] --- Recent STDOUT ---")
                for line in list(self.stdout_buffer)[-50:]:
                    print(line)
                print(f"[{_timestamp()}] --- Recent STDERR ---")
                for line in list(self.stderr_buffer)[-50:]:
                    print(line)
                self.log_line("[Shown recent stdout/stderr]")

            elif choice == "l":
                print(f"Log file: {self.log_path}")

            elif choice == "s":
                # Switch server: stop process, choose, restart and re-init
                try:
                    if self.process and self.process.poll() is None:
                        self.process.terminate()
                        try:
                            self.process.wait(timeout=2)
                        except subprocess.TimeoutExpired:
                            self.process.kill()
                except Exception:
                    pass
                if not self.choose_server_menu():
                    continue
                self.start()
                init_resp = self.initialize()
                if not init_resp or "error" in init_resp:
                    self.print_and_log("Initialize failed on switched server.")
                    continue
                self.send_initialized()

            elif choice == "q":
                return

            else:
                print("Unknown option.")

    # ---------- top-level ----------
    def run(self) -> None:
        self.start()
        init_resp = self.initialize()
        if not init_resp or "error" in init_resp:
            self.print_and_log("Initialize failed; cannot continue.")
            return
        self.send_initialized()
        self.run_menu()
        time.sleep(0.2)
        try:
            if self.process and self.process.poll() is None:
                self.process.terminate()
                try:
                    self.process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    self.process.kill()
        except Exception:
            pass


def show_help() -> None:
    """Display help information with Rich formatting"""
    console = Console(theme=RICH_THEME)

    # Title panel
    title_panel = Panel(
        Align.center(f"[bold]{__file__} - MCP Inspector CLI[/bold]\n\n[dim]A comprehensive command-line tool for inspecting, testing, and debugging MCP (Model Context Protocol) servers.[/dim]"),
        border_style="panel.border",
        padding=(1, 2)
    )
    console.print(title_panel)
    console.print()

    # Usage section
    usage_table = Table(show_header=False, box=None)
    usage_table.add_column("Command", style="bold cyan")
    usage_table.add_column("Description", style="white")

    usage_table.add_row("python mcp-inspector-cli.py", "[dim]Run with default configuration search[/dim]")
    usage_table.add_row("python mcp-inspector-cli.py [CONFIG_FILE]", "[dim]Run with specific config file[/dim]")
    usage_table.add_row("python mcp-inspector-cli.py --help", "[dim]Show this help message[/dim]")

    usage_panel = Panel(
        usage_table,
        title="Usage",
        border_style="cyan",
        padding=(1, 2)
    )
    console.print(usage_panel)
    console.print()

    # Configuration section
    config_example = '''{
  "mcpServers": {
    "my-server": {
      "command": "python",
      "args": ["my_server.py"]
    }
  }
}'''

    config_panel = Panel(
        f"[dim]Configuration files are searched in this order:[/dim]\n"
        "• [cyan]./mcp.json[/cyan] (current directory)\n"
        "• [cyan]~/.cursor/mcp.json[/cyan] (user home)\n\n"
        "[dim]Example configuration:[/dim]",
        title="Configuration",
        border_style="green",
        padding=(1, 2)
    )
    console.print(config_panel)

    # Display JSON example separately
    json_panel = Panel(
        Syntax(config_example, "json", theme="monokai", line_numbers=False),
        border_style="dim green",
        padding=(0, 2)
    )
    console.print(json_panel)
    console.print()

    # Features section
    features = [
        "🔧 Interactive MCP server testing and debugging",
        "🔄 Support for multiple MCP servers in one session",
        "📊 Real-time monitoring of server stdout/stderr",
        "🎨 Rich terminal output with syntax highlighting",
        "📋 Comprehensive logging with color-coded output",
        "🛠️ Tools, Resources, and Prompts testing",
        "📝 Session logging to timestamped files",
        "⏳ Progress indicators for long-running operations",
    ]

    features_text = "\n".join(f"• {feature}" for feature in features)
    features_panel = Panel(
        features_text,
        title="Features",
        border_style="yellow",
        padding=(1, 2)
    )
    console.print(features_panel)
    console.print()

    # Footer
    footer_panel = Panel(
        Align.center("[dim]For more information, visit:[/dim] [link=https://github.com/granludo/mcp-inspector-cli]https://github.com/granludo/mcp-inspector-cli[/link]"),
        border_style="dim blue",
        padding=(1, 2)
    )
    console.print(footer_panel)


def main() -> None:
    """Main entry point with argument parsing"""
    if len(sys.argv) > 1:
        if sys.argv[1] in ("--help", "-h", "help"):
            show_help()
            return

        # Check if it's a config file path
        config_path = sys.argv[1]
        if os.path.exists(config_path):
            tester = MCPTester(config_path)
        else:
            print(f"Error: Configuration file not found: {config_path}")
            print("Use --help for usage information.")
            return
    else:
        # No arguments, use default config search
        tester = MCPTester()

    try:
        tester.run()
    except KeyboardInterrupt:
        print("\n\nInterrupted by user. Exiting...")
    except Exception as e:
        print(f"\nError: {e}")
        print("Use --help for usage information.")


if __name__ == "__main__":
    main()

