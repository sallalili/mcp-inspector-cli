# MCP Inspector CLI

By Ludo (Marc Alier) @granludo

A comprehensive command-line tool for inspecting, testing, and debugging MCP (Model Context Protocol) servers. This tool provides an interactive interface to explore and test all MCP capabilities including tools, resources, and prompts.

Works with STDIO Transport protocol 

## Features

- **Interactive Server Testing**: Connect to MCP servers and test their capabilities through an intuitive menu-driven interface
- **Multiple Server Support**: Configure and switch between multiple MCP servers
- **Comprehensive MCP Protocol Support**:
  - Tools: List available tools and execute them with custom arguments
  - Resources: Browse and read server-provided resources
  - Prompts: List and retrieve prompts with arguments
- **Advanced Logging**: Automatic session logging with timestamps and colored output
- **Flexible Configuration**: Support for various configuration file formats and locations
- **Real-time I/O Monitoring**: View server stdout/stderr output in real-time
- **Timeout Handling**: Intelligent timeout management with user extension options

## Prerequisites

- Python 3.11+
- uv (Python package manager)
- MCP server(s) to test against

## Installation


1. **Clone the repository**:
   ```bash
   git clone git@github.com:granludo/mcp-inspector-cli.git>
   cd mcp-inspector-cli
   ```
2. **create venv**

   ```bash
   uv venv
   source .venv/bin/activate
   uv pip install -r requirements.txt
   ```


3. **Run directly with uv** :
   ```bash
   uv run mcp-inspector-cli.py
   ```

## Configuration

The tool supports multiple configuration methods, checked in order of priority:

### 1. Command Line Argument
```bash
python mcp-inspector-cli.py /path/to/config.json
```

### 2. Local Configuration File
Create `mcp.json` in the same directory as the script:

```json
{
  "mcpServers": {
    "my-server": {
      "command": "python",
      "args": ["my_server.py"]
    }
  }
}
```

### 3. Cursor-style Configuration
Place configuration in `~/.cursor/mcp.json`:

```json
{
  "mcpServers": {
    "filesystem": {
      "command": "npx",
      "args": ["-y", "@modelcontextprotocol/server-filesystem", "/tmp"]
    },
    "git": {
      "command": "npx",
      "args": ["-y", "@modelcontextprotocol/server-git", "--repository", "/path/to/repo"]
    }
  }
}
```

### 4. Single Server Configuration
For a single server, you can use a simpler format:

```json
{
  "command": "uv",
  "args": ["run", "main.py"],
  "name": "my-custom-server"
}
```

## Usage

### Basic Usage

#### If installed with uv:
```bash
mcp-inspector-cli
```

#### If running directly with uv:
```bash
uv run mcp-inspector-cli.py
```

#### With configuration file:
```bash
uv run mcp-inspector-cli.py /path/to/config.json
```

If you have multiple servers configured, you'll be prompted to select one. For single server configurations, it will connect automatically.

### Interactive Menu

Once connected, you'll see the main menu:

```
=== MCP Tester Menu ===
[t] List and call tools
[r] List and read resources
[p] List and get prompts
[o] Show recent stdout/stderr
[l] Show session log path
[s] Switch server
[q] Quit
```

### Testing Tools

1. Select `[t]` to enter the tools submenu
2. View available tools with their descriptions
3. Select a tool by entering its index number
4. Provide arguments based on the tool's input schema
5. View the results and choose to call another tool or return to main menu

### Testing Resources

1. Select `[r]` to list available resources
2. Select a resource by index to read its content
3. The tool will display the resource content

### Testing Prompts

1. Select `[p]` to list available prompts
2. Select a prompt by index
3. Provide arguments if the prompt requires them
4. View the generated prompt content

### Additional Features

- **Output Monitoring** (`[o]`): View recent server stdout/stderr output
- **Session Logging** (`[l]`): Display the path to the current session log file
- **Server Switching** (`[s]`): Stop current server and connect to a different one

## Configuration Examples

### Filesystem Server
```json
{
  "mcpServers": {
    "filesystem": {
      "command": "npx",
      "args": ["-y", "@modelcontextprotocol/server-filesystem", "/home/user"]
    }
  }
}
```

### Git Repository Server
```json
{
  "mcpServers": {
    "git-repo": {
      "command": "npx",
      "args": ["-y", "@modelcontextprotocol/server-git", "--repository", "/path/to/git/repo"]
    }
  }
}
```

### SQLite Database Server
```json
{
  "mcpServers": {
    "sqlite": {
      "command": "npx",
      "args": ["-y", "@modelcontextprotocol/server-sqlite", "--db-path", "/path/to/database.db"]
    }
  }
}
```

### Custom Python Server
```json
{
  "mcpServers": {
    "custom-server": {
      "command": "python",
      "args": ["-m", "my_mcp_server"]
    }
  }
}
```

## Logging

The tool automatically creates session logs in the format `session-YYYYMMDD-HHMMSS.txt` in the working directory. These logs contain:

- All JSON-RPC requests and responses
- Server stdout/stderr output
- User interactions and menu selections
- Timestamps for all operations
- Error messages and debugging information

## Troubleshooting

### Common Issues

1. **"Command not found" Error**
   - Ensure the MCP server command is installed and accessible
   - Check that all required dependencies are installed
   - Verify the command path in your configuration

2. **Connection Timeout**
   - Some servers may take longer to initialize
   - The tool will prompt you to extend the timeout if needed
   - Check server logs for initialization issues

3. **Invalid JSON Responses**
   - Ensure your MCP server is implementing the protocol correctly
   - Check server stderr output for error details
   - Verify the server is compatible with MCP protocol version 2024-11-05

4. **Permission Errors**
   - Ensure you have appropriate permissions to run the server command
   - Check file/directory permissions for server working directories

### Debug Mode

For additional debugging information:
- Use the `[o]` option to view real-time server output
- Check the session log file for detailed request/response information
- Monitor server stderr for error messages

## MCP Protocol Support

This tool implements the MCP (Model Context Protocol) specification and supports:

- **Protocol Version**: 2024-11-05
- **Initialization Handshake**: Proper initialize/initialized sequence
- **Tools**: Complete tools/list and tools/call support
- **Resources**: Full resources/list and resources/read functionality
- **Prompts**: Comprehensive prompts/list and prompts/get capabilities
- **Error Handling**: Proper JSON-RPC error response handling

## Development

The tool is written in Python and consists of a single script with the following main components:

- `MCPTester` class: Main application logic
- Server management and process handling
- JSON-RPC communication layer
- Interactive menu system
- Logging and output formatting

### Extending the Tool

The code is structured to be easily extensible. You can add support for new MCP features by:

1. Adding new request methods in the protocol helpers section
2. Extending the interactive menu system
3. Adding new configuration options

## License

See LICENSE file for details.

## Contributing

Contributions are welcome! Please feel free to submit issues and pull requests.

## Support

For questions, issues, or feature requests:

1. Check the troubleshooting section above
2. Review the session logs for error details
3. Open an issue with relevant log excerpts and configuration details
