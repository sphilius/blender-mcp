# CLAUDE.md - BlenderMCP Developer Guide

**Last Updated**: November 16, 2025
**Version**: 1.1.3
**Repository**: [blender-mcp](https://github.com/ahujasid/blender-mcp)

## Project Overview

BlenderMCP is a Model Context Protocol (MCP) server that bridges Blender 3D modeling software with Claude AI, enabling natural language-driven 3D content creation. Users can describe scenes in plain English, and Claude will manipulate Blender to create them.

### Purpose

Enable AI-assisted 3D modeling by allowing Claude to:
- Inspect and understand Blender scenes
- Execute Python code in Blender's context
- Download and integrate high-quality assets from Poly Haven
- Generate custom 3D models using Hyper3D Rodin AI
- Apply materials, textures, and lighting
- Manage spatial relationships between objects

## Architecture

### Two-Component System

```
┌─────────────────┐         MCP Protocol          ┌──────────────────┐
│  Claude Desktop │ ◄──────────────────────────► │   MCP Server     │
│   or Cursor     │      (stdio/websocket)        │  (server.py)     │
└─────────────────┘                               └──────────────────┘
                                                           │
                                                           │ TCP Socket
                                                           │ (port 9876)
                                                           ▼
                                                   ┌──────────────────┐
                                                   │ Blender Addon    │
                                                   │  (addon.py)      │
                                                   │                  │
                                                   │  ┌────────────┐  │
                                                   │  │  Blender   │  │
                                                   │  │   Python   │  │
                                                   │  │    API     │  │
                                                   │  └────────────┘  │
                                                   └──────────────────┘
```

### Component Details

**1. MCP Server** (`src/blender_mcp/server.py`)
- Implements FastMCP protocol
- Manages persistent TCP connection to Blender
- Exposes tools and prompts to Claude
- Handles timeouts and reconnection logic
- File size: ~750 lines
- Language: Python 3.10+

**2. Blender Addon** (`addon.py`)
- Runs inside Blender as a plugin
- Creates socket server on localhost:9876
- Executes commands in Blender's main thread via `bpy.app.timers`
- Handles Poly Haven and Hyper3D API calls
- File size: ~68KB, extensive functionality
- Blender version: 3.0+

## Available Tools

### Core Tools (Always Available)

**`get_scene_info()`**
- Returns current scene information
- Object count, names, types, locations
- Limited to first 10 objects for performance

**`get_object_info(object_name)`**
- Detailed object information
- **World bounding box** - critical for spatial relationships
- Location, rotation, scale, material info

**`execute_blender_code(code)`**
- Execute arbitrary Python in Blender context
- **CAUTION**: Powerful but potentially dangerous
- Always save work before using
- Returns stdout output (as of PR #97)

### Poly Haven Integration (Optional)

**`get_polyhaven_status()`**
- Check if Poly Haven is enabled

**`get_polyhaven_categories(asset_type)`**
- Browse categories: hdris, textures, models

**`search_polyhaven_assets(asset_type, categories)`**
- Search assets with filters
- Returns sorted by popularity

**`download_polyhaven_asset(asset_id, asset_type, resolution, file_format)`**
- Download and import assets
- HDRIs automatically set as environment
- Textures create materials with PBR maps
- Models imported into scene

**`set_texture(object_name, texture_id)`**
- Apply downloaded texture to object
- Creates material with proper node setup

### Hyper3D Rodin Integration (Optional)

**`get_hyper3d_status()`**
- Check integration availability
- Returns key type (free_trial or custom)

**`generate_hyper3d_model_via_text(text_prompt, bbox_condition)`**
- Text-to-3D generation
- Returns task_uuid and subscription_key for polling
- Free trial: limited generations per day

**`generate_hyper3d_model_via_images(input_image_paths, input_image_urls, bbox_condition)`**
- Image-to-3D generation
- Supports local paths (hyper3d.ai) or URLs (fal.ai)
- Returns request_id for polling

**`poll_rodin_job_status(subscription_key, request_id)`**
- Check generation progress
- Status: "Done", "Failed", "IN_PROGRESS", etc.
- Polling API - only proceed when complete

**`import_generated_asset(name, task_uuid, request_id)`**
- Import completed 3D model
- **Critical**: Always check world_bounding_box after import
- Adjust location, scale, rotation for proper placement

## Asset Creation Strategy

The MCP server includes a sophisticated prompt that guides Claude's decision-making:

```
Priority Order:
1. Check scene with get_scene_info()
2. Verify Poly Haven status
3. Verify Hyper3D status
4. Use external assets (Poly Haven or Hyper3D)
5. Only fall back to primitives when necessary
```

### Why This Matters

- External assets are higher quality than primitives
- Hyper3D is best for single items, not whole scenes
- World bounding boxes ensure proper spatial relationships
- Prevents object clipping and misalignment

## Communication Protocol

### JSON Message Format

**Command (Client → Blender)**
```json
{
  "type": "command_name",
  "params": {
    "param1": "value1",
    "param2": "value2"
  }
}
```

**Response (Blender → Client)**
```json
{
  "status": "success",
  "result": { /* command-specific data */ }
}
```

**Error Response**
```json
{
  "status": "error",
  "message": "Error description"
}
```

### Socket Behavior

- **Chunked responses**: Server collects complete JSON before parsing
- **Timeout**: 15 seconds (matches on both sides)
- **Reconnection**: Automatic on connection loss
- **Buffering**: 8192 bytes per chunk

## Recent Development History

### Major Milestones

**v1.1.0 - Integrations**
- Added Poly Haven API support
- Added Hyper3D Rodin integration
- Enhanced asset creation strategy

**Recent PRs & Commits**
- **PR #97**: Return stdout from execute_code
- **PR #91**: Updated Cursor integration instructions
- **PR #88**: Rodin integration fixes (error messages, GLB import)
- **PR #84**: Fixed PLANE object scale application
- **Refactoring**: Removed create/delete/modify functions, shifted to code execution approach

### Why the Refactoring?

Early versions had specific functions for creating/modifying objects. The project pivoted to a more flexible `execute_blender_code` approach because:
- More powerful and flexible
- Handles edge cases better
- Allows Claude to use full Blender Python API
- Easier to maintain (one tool vs. many specific ones)

## Known Issues & Troubleshooting

### Connection Issues

**Symptom**: "Could not connect to Blender"
- **Fix**: Ensure Blender addon is running (sidebar → BlenderMCP → Connect)
- **Note**: First command sometimes fails, subsequent commands work
- **Workaround**: Restart both Claude and Blender addon

### Timeout Errors

**Symptom**: "Timeout waiting for Blender response"
- **Fix**: Break complex operations into smaller steps
- **Root cause**: 15-second timeout on socket operations
- **Strategy**: Use incremental code execution instead of large blocks

### Poly Haven Erratic Behavior

**Symptom**: Downloads fail or behave inconsistently
- **Fix**: Retry operation
- **Note**: Mentioned in README troubleshooting, likely API rate limits
- **Workaround**: Add delays between asset downloads

### Hyper3D Daily Limits

**Symptom**: "Insufficient balance" with free trial key
- **Fix**: Wait until next day, or get custom API key
- **Options**:
  - hyper3d.ai for personal key
  - fal.ai for alternative platform

### Security Concerns

**Issue**: `execute_blender_code` allows arbitrary code execution
- **Risk**: Malicious or buggy code can crash Blender or corrupt files
- **Mitigation**: Always save work before using
- **Best practice**: Review generated code when possible

## What's Working Well

✅ **Robust Communication**: Chunked response handling prevents partial JSON errors
✅ **Multiple Integrations**: Claude Desktop and Cursor both supported
✅ **Asset Strategy**: Smart prompts guide Claude to make good choices
✅ **Community**: Active Discord, GitHub sponsors, tutorial videos
✅ **Flexibility**: Code execution approach handles diverse use cases
✅ **Spatial Awareness**: World bounding boxes enable proper object placement

## Development Roadmap

### Short-term Improvements

1. **Connection Stability**
   - Fix "first command fails" issue
   - Improve reconnection logic
   - Better timeout handling

2. **Asset Caching**
   - Cache downloaded Poly Haven assets
   - Reuse materials and textures
   - Reduce redundant downloads

3. **Error Messages**
   - More specific error descriptions
   - Debugging information for developers
   - Better user-facing messages

### Medium-term Goals

4. **Enhanced Scene Understanding**
   - Better spatial relationship detection
   - Collision detection before object placement
   - Automatic camera framing

5. **Performance Optimization**
   - Handle scenes with >100 objects
   - Reduce JSON payload sizes
   - Async operations where possible

6. **Additional Integrations**
   - More asset libraries
   - AI texture generation
   - Material marketplaces

### Long-term Vision

7. **Advanced Features**
   - Animation support
   - Physics simulation setup
   - Rendering automation
   - Multi-scene projects

8. **Developer Experience**
   - Better debugging tools
   - Test suite for addon and server
   - CI/CD pipeline

## File Structure

```
blender-mcp/
├── README.md                    # User documentation
├── CLAUDE.md                    # This file - developer guide
├── LICENSE                      # MIT License
├── pyproject.toml              # Python package configuration
├── uv.lock                     # Dependency lock file
├── .python-version             # Python 3.13.2
├── main.py                     # Entry point wrapper
├── addon.py                    # Blender addon (68KB)
├── src/
│   └── blender_mcp/
│       ├── __init__.py
│       └── server.py           # MCP server implementation
└── assets/
    ├── addon-instructions.png  # Setup guide image
    └── hammer-icon.png         # Tool icon

Total commits: 91+
Active contributors: Multiple (community-driven)
```

## Development Guidelines

### Adding New Tools

1. **Add handler in addon.py**
   ```python
   def my_new_tool(self, param1, param2):
       # Implementation
       return {"result": "data"}
   ```

2. **Register in command handlers**
   ```python
   handlers = {
       "my_new_tool": self.my_new_tool,
   }
   ```

3. **Add MCP tool in server.py**
   ```python
   @mcp.tool()
   def my_new_tool(ctx: Context, param1: str, param2: int) -> str:
       blender = get_blender_connection()
       result = blender.send_command("my_new_tool", {
           "param1": param1,
           "param2": param2
       })
       return json.dumps(result)
   ```

### Testing Strategy

- **Manual testing**: Use Claude to invoke new tools
- **Save frequently**: execute_blender_code can crash Blender
- **Check logs**: Both server.py and Blender console output
- **Test edge cases**: Empty scenes, missing objects, etc.

### Code Style

- Python 3.10+ features allowed
- Type hints appreciated but not required
- Descriptive variable names
- Comments for complex logic
- Error handling with try/except

## Integration Setup

### Claude Desktop

Edit `claude_desktop_config.json`:
```json
{
  "mcpServers": {
    "blender": {
      "command": "uvx",
      "args": ["blender-mcp"]
    }
  }
}
```

### Cursor IDE

**Mac**: Settings > MCP or `.cursor/mcp.json`:
```json
{
  "mcpServers": {
    "blender": {
      "command": "uvx",
      "args": ["blender-mcp"]
    }
  }
}
```

**Windows**: Settings > MCP > Add Server:
```json
{
  "mcpServers": {
    "blender": {
      "command": "cmd",
      "args": ["/c", "uvx", "blender-mcp"]
    }
  }
}
```

### Blender Addon Installation

1. Download `addon.py`
2. Blender > Edit > Preferences > Add-ons
3. Install... > Select `addon.py`
4. Enable "Interface: Blender MCP"
5. 3D View sidebar (N) > BlenderMCP > Connect to Claude

## Example Workflows

### Creating a Scene

```
User: "Create a low poly dungeon scene with a dragon guarding gold"

Claude:
1. get_scene_info() - understand current state
2. get_polyhaven_status() - check asset availability
3. search_polyhaven_assets("models", "dungeon,medieval")
4. download_polyhaven_asset("stone_wall_001", "models")
5. generate_hyper3d_model_via_text("fantasy dragon")
6. poll_rodin_job_status(...) - wait for completion
7. import_generated_asset("Dragon", task_uuid)
8. execute_blender_code() - position objects using bounding boxes
```

### Applying Textures

```
User: "Make the floor look like stone"

Claude:
1. get_scene_info() - find floor object
2. search_polyhaven_assets("textures", "stone,floor")
3. download_polyhaven_asset("cobblestone_001", "textures", "2k")
4. set_texture("Floor", "cobblestone_001")
```

## Community & Support

- **Discord**: [Join here](https://discord.gg/z5apgR8TFU)
- **GitHub Issues**: Bug reports and feature requests
- **GitHub Sponsors**: Support development
- **YouTube**: Tutorial videos and demos
- **Author**: [Siddharth Ahuja](https://x.com/sidahuj)

## Contributing

Contributions welcome! Areas of need:
- Connection stability improvements
- Additional asset integrations
- Better error handling
- Documentation and examples
- Test coverage

## License

MIT License - See LICENSE file

---

**Note**: This is a third-party integration, not officially made by Blender Foundation or Anthropic.

**Warning**: Always save your Blender work before using execute_blender_code. The tool can execute arbitrary code and potentially crash Blender or corrupt files.

**Pro Tip**: Start with simple commands to verify connection, then build up to complex scenes incrementally.
