# blender_mcp_server.py
from mcp.server.fastmcp import FastMCP, Context, Image
import socket
import json
import asyncio
import logging
from dataclasses import dataclass
from contextlib import asynccontextmanager
from typing import AsyncIterator, Dict, Any, List
import os
from pathlib import Path
import base64
from urllib.parse import urlparse
import re

from . import gen3d

# Pipeline scripts (decimate, bake, rig, export) can run for minutes; the old 15 s limit
# killed them mid-way. Override with BLENDER_MCP_TIMEOUT (seconds).
SOCKET_TIMEOUT = float(os.environ.get("BLENDER_MCP_TIMEOUT", "180"))
BLENDER_HOST = os.environ.get("BLENDER_HOST", "localhost")
BLENDER_PORT = int(os.environ.get("BLENDER_PORT", "9876"))

# Configure logging
logging.basicConfig(level=logging.INFO, 
                    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger("BlenderMCPServer")

@dataclass
class BlenderConnection:
    host: str
    port: int
    sock: socket.socket = None  # Changed from 'socket' to 'sock' to avoid naming conflict
    
    def connect(self) -> bool:
        """Connect to the Blender addon socket server"""
        if self.sock:
            return True
            
        try:
            self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self.sock.connect((self.host, self.port))
            logger.info(f"Connected to Blender at {self.host}:{self.port}")
            return True
        except Exception as e:
            logger.error(f"Failed to connect to Blender: {str(e)}")
            self.sock = None
            return False
    
    def disconnect(self):
        """Disconnect from the Blender addon"""
        if self.sock:
            try:
                self.sock.close()
            except Exception as e:
                logger.error(f"Error disconnecting from Blender: {str(e)}")
            finally:
                self.sock = None

    def receive_full_response(self, sock, buffer_size=8192):
        """Receive the complete response, potentially in multiple chunks"""
        chunks = []
        # Use a consistent timeout value that matches the addon's timeout
        sock.settimeout(SOCKET_TIMEOUT)
        
        try:
            while True:
                try:
                    chunk = sock.recv(buffer_size)
                    if not chunk:
                        # If we get an empty chunk, the connection might be closed
                        if not chunks:  # If we haven't received anything yet, this is an error
                            raise Exception("Connection closed before receiving any data")
                        break
                    
                    chunks.append(chunk)
                    
                    # Check if we've received a complete JSON object
                    try:
                        data = b''.join(chunks)
                        json.loads(data.decode('utf-8'))
                        # If we get here, it parsed successfully
                        logger.info(f"Received complete response ({len(data)} bytes)")
                        return data
                    except json.JSONDecodeError:
                        # Incomplete JSON, continue receiving
                        continue
                except socket.timeout:
                    # If we hit a timeout during receiving, break the loop and try to use what we have
                    logger.warning("Socket timeout during chunked receive")
                    break
                except (ConnectionError, BrokenPipeError, ConnectionResetError) as e:
                    logger.error(f"Socket connection error during receive: {str(e)}")
                    raise  # Re-raise to be handled by the caller
        except socket.timeout:
            logger.warning("Socket timeout during chunked receive")
        except Exception as e:
            logger.error(f"Error during receive: {str(e)}")
            raise
            
        # If we get here, we either timed out or broke out of the loop
        # Try to use what we have
        if chunks:
            data = b''.join(chunks)
            logger.info(f"Returning data after receive completion ({len(data)} bytes)")
            try:
                # Try to parse what we have
                json.loads(data.decode('utf-8'))
                return data
            except json.JSONDecodeError:
                # If we can't parse it, it's incomplete
                raise Exception("Incomplete JSON response received")
        else:
            raise Exception("No data received")

    def send_command(self, command_type: str, params: Dict[str, Any] = None) -> Dict[str, Any]:
        """Send a command to Blender and return the response"""
        if not self.sock and not self.connect():
            raise ConnectionError("Not connected to Blender")
        
        command = {
            "type": command_type,
            "params": params or {}
        }
        
        try:
            # Log the command being sent
            shown = json.dumps(params or {})
            logger.info(f"Sending command: {command_type} with params: {shown[:300]}{'...' if len(shown) > 300 else ''}")
            
            # Send the command
            self.sock.sendall(json.dumps(command).encode('utf-8'))
            logger.info(f"Command sent, waiting for response...")
            
            # Set a timeout for receiving - use the same timeout as in receive_full_response
            self.sock.settimeout(SOCKET_TIMEOUT)
            
            # Receive the response using the improved receive_full_response method
            response_data = self.receive_full_response(self.sock)
            logger.info(f"Received {len(response_data)} bytes of data")
            
            response = json.loads(response_data.decode('utf-8'))
            logger.info(f"Response parsed, status: {response.get('status', 'unknown')}")
            
            if response.get("status") == "error":
                logger.error(f"Blender error: {response.get('message')}")
                raise Exception(response.get("message", "Unknown error from Blender"))
            
            return response.get("result", {})
        except socket.timeout:
            logger.error("Socket timeout while waiting for response from Blender")
            # Don't try to reconnect here - let the get_blender_connection handle reconnection
            # Just invalidate the current socket so it will be recreated next time
            self.sock = None
            raise Exception("Timeout waiting for Blender response - try simplifying your request")
        except (ConnectionError, BrokenPipeError, ConnectionResetError) as e:
            logger.error(f"Socket connection error: {str(e)}")
            self.sock = None
            raise Exception(f"Connection to Blender lost: {str(e)}")
        except json.JSONDecodeError as e:
            logger.error(f"Invalid JSON response from Blender: {str(e)}")
            # Try to log what was received
            if 'response_data' in locals() and response_data:
                logger.error(f"Raw response (first 200 bytes): {response_data[:200]}")
            raise Exception(f"Invalid response from Blender: {str(e)}")
        except Exception as e:
            logger.error(f"Error communicating with Blender: {str(e)}")
            # Don't try to reconnect here - let the get_blender_connection handle reconnection
            self.sock = None
            raise Exception(f"Communication error with Blender: {str(e)}")

@asynccontextmanager
async def server_lifespan(server: FastMCP) -> AsyncIterator[Dict[str, Any]]:
    """Manage server startup and shutdown lifecycle"""
    # We don't need to create a connection here since we're using the global connection
    # for resources and tools
    
    try:
        # Just log that we're starting up
        logger.info("BlenderMCP server starting up")
        
        # Try to connect to Blender on startup to verify it's available
        try:
            # This will initialize the global connection if needed
            blender = get_blender_connection()
            logger.info("Successfully connected to Blender on startup")
        except Exception as e:
            logger.warning(f"Could not connect to Blender on startup: {str(e)}")
            logger.warning("Make sure the Blender addon is running before using Blender resources or tools")
        
        # Return an empty context - we're using the global connection
        yield {}
    finally:
        # Clean up the global connection on shutdown
        global _blender_connection
        if _blender_connection:
            logger.info("Disconnecting from Blender on shutdown")
            _blender_connection.disconnect()
            _blender_connection = None
        logger.info("BlenderMCP server shut down")

# Create the MCP server with lifespan support
mcp = FastMCP(
    "BlenderMCP",
    description="Blender integration through the Model Context Protocol",
    lifespan=server_lifespan
)

# Resource endpoints

# Global connection for resources (since resources can't access context)
_blender_connection = None
_polyhaven_enabled = False  # Add this global variable

def get_blender_connection():
    """Get or create a persistent Blender connection"""
    global _blender_connection, _polyhaven_enabled  # Add _polyhaven_enabled to globals
    
    # If we have an existing connection, check if it's still valid
    if _blender_connection is not None:
        try:
            # First check if PolyHaven is enabled by sending a ping command
            result = _blender_connection.send_command("get_polyhaven_status")
            # Store the PolyHaven status globally
            _polyhaven_enabled = result.get("enabled", False)
            return _blender_connection
        except Exception as e:
            # Connection is dead, close it and create a new one
            logger.warning(f"Existing connection is no longer valid: {str(e)}")
            try:
                _blender_connection.disconnect()
            except:
                pass
            _blender_connection = None
    
    # Create a new connection if needed
    if _blender_connection is None:
        _blender_connection = BlenderConnection(host=BLENDER_HOST, port=BLENDER_PORT)
        if not _blender_connection.connect():
            logger.error("Failed to connect to Blender")
            _blender_connection = None
            raise Exception("Could not connect to Blender. Make sure the Blender addon is running.")
        logger.info("Created new persistent connection to Blender")
    
    return _blender_connection


@mcp.tool()
def get_scene_info(ctx: Context) -> str:
    """Get detailed information about the current Blender scene"""
    try:
        blender = get_blender_connection()
        result = blender.send_command("get_scene_info")
        
        # Just return the JSON representation of what Blender sent us
        return json.dumps(result, indent=2)
    except Exception as e:
        logger.error(f"Error getting scene info from Blender: {str(e)}")
        return f"Error getting scene info: {str(e)}"

@mcp.tool()
def get_object_info(ctx: Context, object_name: str) -> str:
    """
    Get detailed information about a specific object in the Blender scene.
    
    Parameters:
    - object_name: The name of the object to get information about
    """
    try:
        blender = get_blender_connection()
        result = blender.send_command("get_object_info", {"name": object_name})
        
        # Just return the JSON representation of what Blender sent us
        return json.dumps(result, indent=2)
    except Exception as e:
        logger.error(f"Error getting object info from Blender: {str(e)}")
        return f"Error getting object info: {str(e)}"



@mcp.tool()
def execute_blender_code(ctx: Context, code: str) -> str:
    """
    Execute arbitrary Python code in Blender. Make sure to do it step-by-step by breaking it into smaller chunks.
    
    Parameters:
    - code: The Python code to execute
    """
    try:
        # Get the global connection
        blender = get_blender_connection()
        
        result = blender.send_command("execute_code", {"code": code})
        return f"Code executed successfully: {result.get('result', '')}"
    except Exception as e:
        logger.error(f"Error executing code: {str(e)}")
        return f"Error executing code: {str(e)}"

@mcp.tool()
def get_polyhaven_categories(ctx: Context, asset_type: str = "hdris") -> str:
    """
    Get a list of categories for a specific asset type on Polyhaven.
    
    Parameters:
    - asset_type: The type of asset to get categories for (hdris, textures, models, all)
    """
    try:
        blender = get_blender_connection()
        if not _polyhaven_enabled:
            return "PolyHaven integration is disabled. Select it in the sidebar in BlenderMCP, then run it again."
        result = blender.send_command("get_polyhaven_categories", {"asset_type": asset_type})
        
        if "error" in result:
            return f"Error: {result['error']}"
        
        # Format the categories in a more readable way
        categories = result["categories"]
        formatted_output = f"Categories for {asset_type}:\n\n"
        
        # Sort categories by count (descending)
        sorted_categories = sorted(categories.items(), key=lambda x: x[1], reverse=True)
        
        for category, count in sorted_categories:
            formatted_output += f"- {category}: {count} assets\n"
        
        return formatted_output
    except Exception as e:
        logger.error(f"Error getting Polyhaven categories: {str(e)}")
        return f"Error getting Polyhaven categories: {str(e)}"

@mcp.tool()
def search_polyhaven_assets(
    ctx: Context,
    asset_type: str = "all",
    categories: str = None
) -> str:
    """
    Search for assets on Polyhaven with optional filtering.
    
    Parameters:
    - asset_type: Type of assets to search for (hdris, textures, models, all)
    - categories: Optional comma-separated list of categories to filter by
    
    Returns a list of matching assets with basic information.
    """
    try:
        blender = get_blender_connection()
        result = blender.send_command("search_polyhaven_assets", {
            "asset_type": asset_type,
            "categories": categories
        })
        
        if "error" in result:
            return f"Error: {result['error']}"
        
        # Format the assets in a more readable way
        assets = result["assets"]
        total_count = result["total_count"]
        returned_count = result["returned_count"]
        
        formatted_output = f"Found {total_count} assets"
        if categories:
            formatted_output += f" in categories: {categories}"
        formatted_output += f"\nShowing {returned_count} assets:\n\n"
        
        # Sort assets by download count (popularity)
        sorted_assets = sorted(assets.items(), key=lambda x: x[1].get("download_count", 0), reverse=True)
        
        for asset_id, asset_data in sorted_assets:
            formatted_output += f"- {asset_data.get('name', asset_id)} (ID: {asset_id})\n"
            formatted_output += f"  Type: {['HDRI', 'Texture', 'Model'][asset_data.get('type', 0)]}\n"
            formatted_output += f"  Categories: {', '.join(asset_data.get('categories', []))}\n"
            formatted_output += f"  Downloads: {asset_data.get('download_count', 'Unknown')}\n\n"
        
        return formatted_output
    except Exception as e:
        logger.error(f"Error searching Polyhaven assets: {str(e)}")
        return f"Error searching Polyhaven assets: {str(e)}"

@mcp.tool()
def download_polyhaven_asset(
    ctx: Context,
    asset_id: str,
    asset_type: str,
    resolution: str = "1k",
    file_format: str = None
) -> str:
    """
    Download and import a Polyhaven asset into Blender.
    
    Parameters:
    - asset_id: The ID of the asset to download
    - asset_type: The type of asset (hdris, textures, models)
    - resolution: The resolution to download (e.g., 1k, 2k, 4k)
    - file_format: Optional file format (e.g., hdr, exr for HDRIs; jpg, png for textures; gltf, fbx for models)
    
    Returns a message indicating success or failure.
    """
    try:
        blender = get_blender_connection()
        result = blender.send_command("download_polyhaven_asset", {
            "asset_id": asset_id,
            "asset_type": asset_type,
            "resolution": resolution,
            "file_format": file_format
        })
        
        if "error" in result:
            return f"Error: {result['error']}"
        
        if result.get("success"):
            message = result.get("message", "Asset downloaded and imported successfully")
            
            # Add additional information based on asset type
            if asset_type == "hdris":
                return f"{message}. The HDRI has been set as the world environment."
            elif asset_type == "textures":
                material_name = result.get("material", "")
                maps = ", ".join(result.get("maps", []))
                return f"{message}. Created material '{material_name}' with maps: {maps}."
            elif asset_type == "models":
                return f"{message}. The model has been imported into the current scene."
            else:
                return message
        else:
            return f"Failed to download asset: {result.get('message', 'Unknown error')}"
    except Exception as e:
        logger.error(f"Error downloading Polyhaven asset: {str(e)}")
        return f"Error downloading Polyhaven asset: {str(e)}"

@mcp.tool()
def set_texture(
    ctx: Context,
    object_name: str,
    texture_id: str
) -> str:
    """
    Apply a previously downloaded Polyhaven texture to an object.
    
    Parameters:
    - object_name: Name of the object to apply the texture to
    - texture_id: ID of the Polyhaven texture to apply (must be downloaded first)
    
    Returns a message indicating success or failure.
    """
    try:
        # Get the global connection
        blender = get_blender_connection()
        
        result = blender.send_command("set_texture", {
            "object_name": object_name,
            "texture_id": texture_id
        })
        
        if "error" in result:
            return f"Error: {result['error']}"
        
        if result.get("success"):
            material_name = result.get("material", "")
            maps = ", ".join(result.get("maps", []))
            
            # Add detailed material info
            material_info = result.get("material_info", {})
            node_count = material_info.get("node_count", 0)
            has_nodes = material_info.get("has_nodes", False)
            texture_nodes = material_info.get("texture_nodes", [])
            
            output = f"Successfully applied texture '{texture_id}' to {object_name}.\n"
            output += f"Using material '{material_name}' with maps: {maps}.\n\n"
            output += f"Material has nodes: {has_nodes}\n"
            output += f"Total node count: {node_count}\n\n"
            
            if texture_nodes:
                output += "Texture nodes:\n"
                for node in texture_nodes:
                    output += f"- {node['name']} using image: {node['image']}\n"
                    if node['connections']:
                        output += "  Connections:\n"
                        for conn in node['connections']:
                            output += f"    {conn}\n"
            else:
                output += "No texture nodes found in the material.\n"
            
            return output
        else:
            return f"Failed to apply texture: {result.get('message', 'Unknown error')}"
    except Exception as e:
        logger.error(f"Error applying texture: {str(e)}")
        return f"Error applying texture: {str(e)}"

@mcp.tool()
def get_polyhaven_status(ctx: Context) -> str:
    """
    Check if PolyHaven integration is enabled in Blender.
    Returns a message indicating whether PolyHaven features are available.
    """
    try:
        blender = get_blender_connection()
        result = blender.send_command("get_polyhaven_status")
        enabled = result.get("enabled", False)
        message = result.get("message", "")
        
        return message
    except Exception as e:
        logger.error(f"Error checking PolyHaven status: {str(e)}")
        return f"Error checking PolyHaven status: {str(e)}"

@mcp.tool()
def get_hyper3d_status(ctx: Context) -> str:
    """
    Check if Hyper3D Rodin integration is enabled in Blender.
    Returns a message indicating whether Hyper3D Rodin features are available.

    Don't emphasize the key type in the returned message, but sliently remember it. 
    """
    try:
        blender = get_blender_connection()
        result = blender.send_command("get_hyper3d_status")
        enabled = result.get("enabled", False)
        message = result.get("message", "")
        if enabled:
            message += ""
        return message
    except Exception as e:
        logger.error(f"Error checking Hyper3D status: {str(e)}")
        return f"Error checking Hyper3D status: {str(e)}"

def _process_bbox(original_bbox: list[float] | list[int] | None) -> list[int] | None:
    if original_bbox is None:
        return None
    if all(isinstance(i, int) for i in original_bbox):
        return original_bbox
    if any(i<=0 for i in original_bbox):
        raise ValueError("Incorrect number range: bbox must be bigger than zero!")
    return [int(float(i) / max(original_bbox) * 100) for i in original_bbox] if original_bbox else None

@mcp.tool()
def generate_hyper3d_model_via_text(
    ctx: Context,
    text_prompt: str,
    bbox_condition: list[float]=None,
    tier: str="Sketch",
    mesh_mode: str="Raw",
) -> str:
    """
    Generate 3D asset using Hyper3D by giving description of the desired asset, and import the asset into Blender.
    The 3D asset has built-in materials.
    The generated model has a normalized size, so re-scaling after generation can be useful.
    
    Parameters:
    - text_prompt: A short description of the desired model in **English**.
    - bbox_condition: Optional. If given, it has to be a list of floats of length 3. Controls the ratio between [Length, Width, Height] of the model.
    - tier: Rodin tier, e.g. "Sketch" (fast, cheapest, default), "Regular", "Detail", "Smooth", "Gen-2".
    - mesh_mode: "Raw" (triangles, default) or "Quad" (quad-dominant, better for rigging and cleanup).

    Returns a message indicating success or failure.
    """
    try:
        blender = get_blender_connection()
        result = blender.send_command("create_rodin_job", {
            "text_prompt": text_prompt,
            "images": None,
            "bbox_condition": _process_bbox(bbox_condition),
            "tier": tier,
            "mesh_mode": mesh_mode,
        })
        succeed = result.get("submit_time", False)
        if succeed:
            return json.dumps({
                "task_uuid": result["uuid"],
                "subscription_key": result["jobs"]["subscription_key"],
            })
        else:
            return json.dumps(result)
    except Exception as e:
        logger.error(f"Error generating Hyper3D task: {str(e)}")
        return f"Error generating Hyper3D task: {str(e)}"

@mcp.tool()
def generate_hyper3d_model_via_images(
    ctx: Context,
    input_image_paths: list[str]=None,
    input_image_urls: list[str]=None,
    bbox_condition: list[float]=None,
    tier: str="Sketch",
    mesh_mode: str="Raw",
) -> str:
    """
    Generate 3D asset using Hyper3D by giving images of the wanted asset, and import the generated asset into Blender.
    The 3D asset has built-in materials.
    The generated model has a normalized size, so re-scaling after generation can be useful.
    
    Parameters:
    - input_image_paths: The **absolute** paths of input images. Even if only one image is provided, wrap it into a list. Required if Hyper3D Rodin in MAIN_SITE mode.
    - input_image_urls: The URLs of input images. Even if only one image is provided, wrap it into a list. Required if Hyper3D Rodin in FAL_AI mode.
    - bbox_condition: Optional. If given, it has to be a list of ints of length 3. Controls the ratio between [Length, Width, Height] of the model.
    - tier: Rodin tier, e.g. "Sketch" (default), "Regular", "Detail", "Smooth", "Gen-2".
    - mesh_mode: "Raw" (triangles, default) or "Quad" (quad-dominant, better for rigging and cleanup).

    Only one of {input_image_paths, input_image_urls} should be given at a time, depending on the Hyper3D Rodin's current mode.
    Returns a message indicating success or failure.
    """
    if input_image_paths is not None and input_image_urls is not None:
        return f"Error: Conflict parameters given!"
    if input_image_paths is None and input_image_urls is None:
        return f"Error: No image given!"
    if input_image_paths is not None:
        if not all(os.path.exists(i) for i in input_image_paths):
            return "Error: not all image paths are valid!"
        images = []
        for path in input_image_paths:
            with open(path, "rb") as f:
                images.append(
                    (Path(path).suffix, base64.b64encode(f.read()).decode("ascii"))
                )
    elif input_image_urls is not None:
        if not all(urlparse(i).scheme in ("http", "https") for i in input_image_urls):
            return "Error: not all image URLs are valid!"
        images = input_image_urls.copy()
    try:
        blender = get_blender_connection()
        result = blender.send_command("create_rodin_job", {
            "text_prompt": None,
            "images": images,
            "bbox_condition": _process_bbox(bbox_condition),
            "tier": tier,
            "mesh_mode": mesh_mode,
        })
        succeed = result.get("submit_time", False)
        if succeed:
            return json.dumps({
                "task_uuid": result["uuid"],
                "subscription_key": result["jobs"]["subscription_key"],
            })
        else:
            return json.dumps(result)
    except Exception as e:
        logger.error(f"Error generating Hyper3D task: {str(e)}")
        return f"Error generating Hyper3D task: {str(e)}"

@mcp.tool()
def poll_rodin_job_status(
    ctx: Context,
    subscription_key: str=None,
    request_id: str=None,
):
    """
    Check if the Hyper3D Rodin generation task is completed.

    For Hyper3D Rodin mode MAIN_SITE:
        Parameters:
        - subscription_key: The subscription_key given in the generate model step.

        Returns a list of status. The task is done if all status are "Done".
        If "Failed" showed up, the generating process failed.
        This is a polling API, so only proceed if the status are finally determined ("Done" or "Canceled").

    For Hyper3D Rodin mode FAL_AI:
        Parameters:
        - request_id: The request_id given in the generate model step.

        Returns the generation task status. The task is done if status is "COMPLETED".
        The task is in progress if status is "IN_PROGRESS".
        If status other than "COMPLETED", "IN_PROGRESS", "IN_QUEUE" showed up, the generating process might be failed.
        This is a polling API, so only proceed if the status are finally determined ("COMPLETED" or some failed state).
    """
    try:
        blender = get_blender_connection()
        kwargs = {}
        if subscription_key:
            kwargs = {
                "subscription_key": subscription_key,
            }
        elif request_id:
            kwargs = {
                "request_id": request_id,
            }
        result = blender.send_command("poll_rodin_job_status", kwargs)
        return result
    except Exception as e:
        logger.error(f"Error generating Hyper3D task: {str(e)}")
        return f"Error generating Hyper3D task: {str(e)}"

@mcp.tool()
def import_generated_asset(
    ctx: Context,
    name: str,
    task_uuid: str=None,
    request_id: str=None,
):
    """
    Import the asset generated by Hyper3D Rodin after the generation task is completed.

    Parameters:
    - name: The name of the object in scene
    - task_uuid: For Hyper3D Rodin mode MAIN_SITE: The task_uuid given in the generate model step.
    - request_id: For Hyper3D Rodin mode FAL_AI: The request_id given in the generate model step.

    Only give one of {task_uuid, request_id} based on the Hyper3D Rodin Mode!
    Return if the asset has been imported successfully.
    """
    try:
        blender = get_blender_connection()
        kwargs = {
            "name": name
        }
        if task_uuid:
            kwargs["task_uuid"] = task_uuid
        elif request_id:
            kwargs["request_id"] = request_id
        result = blender.send_command("import_generated_asset", kwargs)
        return result
    except Exception as e:
        logger.error(f"Error generating Hyper3D task: {str(e)}")
        return f"Error generating Hyper3D task: {str(e)}"

# ----------------------------------------------------------------------------- viewport

@mcp.tool()
def get_viewport_screenshot(ctx: Context, max_size: int = 800) -> Image:
    """
    Capture the current Blender 3D viewport as an image so you can see what you built.

    Use it after every visible change (import, cleanup, rig, pose) to check the result
    instead of guessing from numbers. Needs Blender running with its UI; for headless
    runs use run_script_file with render_preview.py instead.

    Parameters:
    - max_size: longest side of the returned image in pixels (default 800)
    """
    # The add-on returns the PNG bytes over the socket, so this also works when Blender runs
    # on another machine (BLENDER_HOST) with no shared filesystem.
    blender = get_blender_connection()
    result = blender.send_command("get_viewport_screenshot", {"max_size": max_size})
    return Image(data=base64.b64decode(result["image_base64"]), format="png")


# ----------------------------------------------------------------------------- pipeline scripts

_RESULT_MARKER = "PIPELINE_RESULT "


def _scripts_dir() -> str | None:
    return os.environ.get("BLENDER_MCP_SCRIPTS_DIR")


@mcp.tool()
def list_pipeline_scripts(ctx: Context, directory: str = None) -> str:
    """
    List Blender pipeline scripts (cleanup, rigging, baking, export...) available to run_script_file.

    Parameters:
    - directory: folder to scan. Defaults to the BLENDER_MCP_SCRIPTS_DIR environment variable,
      e.g. <game-dev-starter-kit>/pipeline3d/blender

    Returns each script's path and the first line of its docstring.
    """
    folder = directory or _scripts_dir()
    if not folder or not os.path.isdir(os.path.expanduser(folder)):
        return "No scripts folder. Pass directory=... or set BLENDER_MCP_SCRIPTS_DIR in the MCP server config."
    folder = os.path.expanduser(folder)
    rows = []
    for name in sorted(os.listdir(folder)):
        if not name.endswith(".py") or name.startswith("_"):
            continue
        path = os.path.join(folder, name)
        with open(path, encoding="utf-8") as fh:
            head = fh.read(2000)
        doc = re.search(r'"""\s*(.+?)\n', head)
        rows.append({"script": path, "summary": doc.group(1).strip() if doc else ""})
    return json.dumps(rows, indent=2)


@mcp.tool()
def run_script_file(ctx: Context, script_path: str, config: dict = None) -> str:
    """
    Run a pipeline script from disk inside Blender and return its JSON report.

    The script must define main(config) -> dict (all pipeline3d scripts do). The file is read
    by the MCP server and executed in the live Blender session, so the scene changes are
    visible immediately. Only config keys you pass override the script's CONFIG defaults.

    Typical order for an AI-generated asset:
      cleanup_for_godot.py -> (quadruped_rig.py | Mixamo) -> merge_clips.py -> export_for_godot.py
    Also: build_karambit.py, transfer_weights.py, udim_to_01.py, bake_diffuse.py, render_preview.py

    Parameters:
    - script_path: absolute path, or a file name inside BLENDER_MCP_SCRIPTS_DIR
    - config: dict of CONFIG overrides, e.g. {"import_path": "~/in.glb", "target_tris": 12000}
    """
    path = os.path.expanduser(script_path)
    if not os.path.isabs(path) and _scripts_dir():
        path = os.path.join(os.path.expanduser(_scripts_dir()), path)
    if not os.path.exists(path):
        return f"Error: script not found: {path}"
    with open(path, encoding="utf-8") as fh:
        source = fh.read()
    cfg_literal = json.dumps(json.dumps(config or {}))
    code = (f"{source}\n\n"
            f"import json as _pipeline_json\n"
            f"_pipeline_result = main(_pipeline_json.loads({cfg_literal}))\n"
            f"print({_RESULT_MARKER!r} + _pipeline_json.dumps(_pipeline_result, default=str))\n")
    try:
        blender = get_blender_connection()
        result = blender.send_command("execute_code", {"code": code})
        output = result.get("result", "") if isinstance(result, dict) else str(result)
        marker = output.rfind(_RESULT_MARKER)
        if marker == -1:
            return f"Script ran but returned no report. Output:\n{output[-3000:]}"
        log = output[:marker].strip()
        report = output[marker + len(_RESULT_MARKER):].strip()
        return report if not log else f"{report}\n\n--- log ---\n{log[-2000:]}"
    except Exception as e:
        logger.error(f"Error running script {path}: {str(e)}")
        return f"Error running script {path}: {str(e)}"


# ----------------------------------------------------------------------------- cloud generation APIs

def _gen3d_call(args: list[str]) -> dict:
    import io
    from contextlib import redirect_stdout
    buf = io.StringIO()
    try:
        with redirect_stdout(buf):
            gen3d.main(args)
    except SystemExit as exc:
        # argparse rejects bad values (e.g. an unknown provider) with SystemExit; report, don't exit
        return {"ok": False, "error": f"invalid arguments for gen3d ({exc.code}): {' '.join(args[:4])}; "
                                      f"provider must be one of {sorted(gen3d.PROVIDERS)}, "
                                      "mode one of text, image, multiview, refine"}
    return json.loads(buf.getvalue() or "{}")


@mcp.tool()
def generate_3d_via_api(
    ctx: Context,
    provider: str,
    mode: str = "image",
    prompt: str = None,
    image_paths: list[str] = None,
    options: dict = None,
) -> str:
    """
    Start a cloud 3D generation job on Tripo or Meshy (or Rodin with your own key).
    Non-blocking: returns a task id. Then call poll_3d_api_task, then import_3d_api_result.

    Keys come from the MCP server's environment: TRIPO_API_KEY, MESHY_API_KEY, RODIN_API_KEY.
    (Rodin via the Blender addon's free-trial key: use generate_hyper3d_model_via_* instead.)

    Parameters:
    - provider: "tripo" | "meshy" | "rodin"
    - mode: "text" | "image" | "multiview" (multiview images in order front, left, back, right)
    - prompt: text prompt (text mode)
    - image_paths: absolute paths to reference images (image / multiview mode)
    - options: provider request fields passed through unchanged, e.g.
        tripo: {"face_limit": 15000, "quad": true, "texture": false}
        meshy: {"topology": "quad", "target_polycount": 15000, "should_remesh": true}
        rodin: {"tier": "Gen-2", "mesh_mode": "Quad"}

    Game-asset rules: generate a neutral A-pose, flat-lit, weapon-free body; generate armour,
    clothing and props as separate parts; ask for quads and a face budget up front.
    """
    args = ["create", "--provider", provider, "--mode", mode]
    if prompt:
        args += ["--prompt", prompt]
    for path in image_paths or []:
        args += ["--image", os.path.expanduser(path)]
    for k, v in (options or {}).items():
        args += ["--opt", f"{k}={json.dumps(v)}"]
    return json.dumps(_gen3d_call(args), indent=2)


@mcp.tool()
def poll_3d_api_task(ctx: Context, provider: str, task_id: str) -> str:
    """
    Check a Tripo / Meshy / Rodin task started with generate_3d_via_api or rig_3d_via_api.
    Poll every ~10 s until state is success / SUCCEEDED / Done, then call import_3d_api_result.

    Parameters:
    - provider: "tripo" | "meshy" | "rodin"
    - task_id: the "task" value returned when the job was created
    """
    return json.dumps(_gen3d_call(["status", "--provider", provider, "--task", task_id]), indent=2)


@mcp.tool()
def rig_3d_via_api(ctx: Context, provider: str, task_id: str, options: dict = None) -> str:
    """
    Auto-rig a model generated on Tripo or Meshy (humanoids; Tripo also offers other rig types).
    Non-blocking: returns a new task id to poll, then import with import_3d_api_result.
    For quadrupeds without API support, run quadruped_rig.py with run_script_file instead.

    Parameters:
    - provider: "tripo" | "meshy"
    - task_id: the finished generation task id
    - options: provider fields, e.g. meshy {"height_meters": 1.78}
    """
    try:
        p = gen3d.PROVIDERS[provider]()
        return json.dumps({"ok": True, "provider": provider, "task": p.rig(task_id, options or {})})
    except Exception as e:
        return json.dumps({"ok": False, "error": str(e)})


@mcp.tool()
def import_3d_api_result(
    ctx: Context,
    provider: str,
    task_id: str,
    name: str,
    download_dir: str = None,
    prefer: list[str] = None,
) -> str:
    """
    Download a finished Tripo / Meshy / Rodin result and import it into the Blender scene.

    Parameters:
    - provider: "tripo" | "meshy" | "rodin"
    - task_id: finished task id
    - name: file / object name to use (e.g. "character_raw")
    - download_dir: where to keep the file (default: ~/pipeline3d_downloads)
    - prefer: result fields to prefer, e.g. ["pbr_model"] or ["model"] (Tripo), ["fbx"]

    After importing, run cleanup_for_godot.py (run_script_file) before rigging.
    """
    folder = os.path.expanduser(download_dir or "~/pipeline3d_downloads")
    out = os.path.join(folder, f"{name}.glb")   # renamed by gen3d if the result is FBX/OBJ/ZIP
    args = ["download", "--provider", provider, "--task", task_id, "--out", out]
    for field in prefer or []:
        args += ["--prefer", field]
    res = _gen3d_call(args)
    if not res.get("ok"):
        return json.dumps(res, indent=2)
    path = res["out"]
    importer = {"glb": "bpy.ops.import_scene.gltf", "gltf": "bpy.ops.import_scene.gltf",
                "fbx": "bpy.ops.import_scene.fbx", "obj": "bpy.ops.wm.obj_import"}.get(res.get("format"))
    if importer is None:
        res["blender"] = f"downloaded {path} but .{res.get('format')} can't be imported directly; unpack it first"
        return json.dumps(res, indent=2)
    if BLENDER_HOST in ("localhost", "127.0.0.1", "::1"):
        fetch = f"path = {path!r}\n"
    else:
        # Blender is on another machine: let it fetch the same result URL itself.
        suffix = "." + res["format"]
        fetch = ("import tempfile, urllib.request\n"
                 f"path = tempfile.mkstemp(suffix={suffix!r})[1]\n"
                 f"urllib.request.urlretrieve({res['url']!r}, path)\n")
    code = (
        "import bpy\n"
        + fetch +
        "before = set(bpy.data.objects)\n"
        f"{importer}(filepath=path)\n"
        "new = [o.name for o in bpy.data.objects if o not in before]\n"
        "print('IMPORTED', new)\n"
    )
    try:
        imported = get_blender_connection().send_command("execute_code", {"code": code})
        res["blender"] = imported.get("result", "") if isinstance(imported, dict) else str(imported)
    except Exception as e:
        res["blender"] = f"downloaded but not imported: {e}"
    return json.dumps(res, indent=2)


@mcp.prompt()
def game_asset_pipeline() -> str:
    """Stage order and rules for turning generated 3D models into Godot-ready game assets"""
    return """You are running a Blender -> Godot game asset pipeline. Work in stages and check each one.

    0. get_scene_info(); list_pipeline_scripts() to find cleanup / rig / export scripts.
    1. GENERATE one part at a time: body (neutral A-pose, flat lighting, no weapon), then clothing,
       armour and props separately. Prefer quads and a face budget (10-25k for characters).
       Tools: generate_3d_via_api (tripo, meshy), generate_hyper3d_model_via_* (Rodin trial key),
       PolyHaven for environment assets. Hard-surface props with exact features (rings, holes,
       pivots) are better built procedurally (e.g. build_karambit.py) than generated.
    2. CLEAN before rigging: run_script_file("cleanup_for_godot.py", {...}) - join, weld, fill holes,
       drop floaters, decimate to budget, scale to real height, origin at the feet.
       Read the report: tris, dimensions_m, pieces, non_manifold_edges, uv.
    3. LOOK: get_viewport_screenshot() after every visible change. Numbers can pass while the
       mesh is wrong (fused arms, missing hands).
    4. RIG: humanoids -> Mixamo / AccuRIG (manual web/desktop step: tell the user exactly what to
       upload and where to save) or rig_3d_via_api. Quadrupeds -> quadruped_rig.py (31 bones,
       idle/walk/attack/death at 30 fps, in place). Clothing -> transfer_weights.py from the body.
    5. ANIMATE: merge_clips.py for Mixamo / ActorCore / mocap files (one skeleton, one clip per file,
       In Place for locomotion). Keep clips in place: the game controller moves the character.
    6. EXPORT: export_for_godot.py -> .glb, +Y up, one animation per NLA track; collision
       "convex" for props. Godot's pipeline_import plugin sets loop modes by clip name.
    Never apply transforms or join meshes on an already-skinned character (it breaks the skin).
    Normal maps: Blender and Godot are OpenGL (Y+); only flip green for Unreal.
    """

@mcp.prompt()
def asset_creation_strategy() -> str:
    """Defines the preferred strategy for creating assets in Blender"""
    return """When creating 3D content in Blender, always start by checking if integrations are available:

    0. Before anything, always check the scene from get_scene_info()
    1. First use the following tools to verify if the following integrations are enabled:
        1. PolyHaven
            Use get_polyhaven_status() to verify its status
            If PolyHaven is enabled:
            - For objects/models: Use download_polyhaven_asset() with asset_type="models"
            - For materials/textures: Use download_polyhaven_asset() with asset_type="textures"
            - For environment lighting: Use download_polyhaven_asset() with asset_type="hdris"
        2. Hyper3D(Rodin)
            Hyper3D Rodin is good at generating 3D models for single item.
            So don't try to:
            1. Generate the whole scene with one shot
            2. Generate ground using Hyper3D
            3. Generate parts of the items separately and put them together afterwards

            Use get_hyper3d_status() to verify its status
            If Hyper3D is enabled:
            - For objects/models, do the following steps:
                1. Create the model generation task
                    - Use generate_hyper3d_model_via_images() if image(s) is/are given
                    - Use generate_hyper3d_model_via_text() if generating 3D asset using text prompt
                    If key type is free_trial and insufficient balance error returned, tell the user that the free trial key can only generated limited models everyday, they can choose to:
                    - Wait for another day and try again
                    - Go to hyper3d.ai to find out how to get their own API key
                    - Go to fal.ai to get their own private API key
                2. Poll the status
                    - Use poll_rodin_job_status() to check if the generation task has completed or failed
                3. Import the asset
                    - Use import_generated_asset() to import the generated GLB model the asset
                4. After importing the asset, ALWAYS check the world_bounding_box of the imported mesh, and adjust the mesh's location and size
                    Adjust the imported mesh's location, scale, rotation, so that the mesh is on the right spot.

                You can reuse assets previous generated by running python code to duplicate the object, without creating another generation task.

    3. Always check the world_bounding_box for each item so that:
        - Ensure that all objects that should not be clipping are not clipping.
        - Items have right spatial relationship.
    

    Only fall back to scripting when:
    - PolyHaven and Hyper3D are disabled
    - A simple primitive is explicitly requested
    - No suitable PolyHaven asset exists
    - Hyper3D Rodin failed to generate the desired asset
    - The task specifically requires a basic material/color
    """

# Main execution

def main():
    """Run the MCP server"""
    mcp.run()

if __name__ == "__main__":
    main()