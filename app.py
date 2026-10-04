import io
import json
import uuid
from pathlib import Path
from urllib.parse import urlparse

import requests
import streamlit as st
import websocket
from PIL import Image


DEFAULT_COMFY_URL = "http://127.0.0.1:8188"
WORKFLOW_PATH = Path(__file__).with_name("workflow.json")
PREPROCESSORS = (
    "CannyEdgePreprocessor",
    "DepthAnythingPreprocessor",
    "OpenposePreprocessor",
)
PREPROCESSOR_HELP = {
    "CannyEdgePreprocessor": "Preserves strong outlines and composition.",
    "DepthAnythingPreprocessor": "Preserves scene depth and spatial layout.",
    "OpenposePreprocessor": "Preserves human pose and body placement.",
}


def api_url(comfy_url: str, path: str) -> str:
    return f"{comfy_url.rstrip('/')}{path}"


def websocket_url(comfy_url: str, client_id: str) -> str:
    parsed = urlparse(comfy_url)
    scheme = "wss" if parsed.scheme == "https" else "ws"
    return f"{scheme}://{parsed.netloc}/ws?clientId={client_id}"


def upload_image(comfy_url: str, uploaded_file, filename: str) -> str:
    response = requests.post(
        api_url(comfy_url, "/upload/image"),
        files={"image": (filename, uploaded_file.getvalue(), uploaded_file.type)},
        data={"overwrite": "true"},
        timeout=60,
    )
    response.raise_for_status()
    data = response.json()
    return "/".join(part for part in (data.get("subfolder"), data["name"]) if part)


def build_workflow(image_name: str, prompt: str, preprocessor: str, prefix: str) -> dict:
    workflow = json.loads(WORKFLOW_PATH.read_text(encoding="utf-8"))
    workflow["50"]["inputs"]["image"] = image_name
    workflow["45"]["inputs"]["text"] = prompt
    workflow["56"]["inputs"]["preprocessor"] = preprocessor
    workflow["9"]["inputs"]["filename_prefix"] = prefix
    return workflow


def queue_prompt(comfy_url: str, workflow: dict, client_id: str) -> str:
    response = requests.post(
        api_url(comfy_url, "/prompt"),
        json={"prompt": workflow, "client_id": client_id},
        timeout=60,
    )
    response.raise_for_status()
    data = response.json()
    if "error" in data:
        raise RuntimeError(data["error"])
    return data["prompt_id"]


def get_image(comfy_url: str, image_info: dict) -> bytes:
    response = requests.get(api_url(comfy_url, "/view"), params=image_info, timeout=120)
    response.raise_for_status()
    return response.content


def show_preview(preview, image_data: bytes | Image.Image, preprocessor: str) -> None:
    if isinstance(image_data, bytes):
        with Image.open(io.BytesIO(image_data)) as image:
            image.load()
            image_data = image.copy()
    preview.image(image_data, caption=f"{preprocessor} preview", use_container_width=True)


def run_generation(comfy_url: str, workflow: dict, preprocessor: str, status, progress, preview):
    client_id = str(uuid.uuid4())
    ws = websocket.create_connection(websocket_url(comfy_url, client_id), timeout=10)
    ws.settimeout(300)
    try:
        prompt_id = queue_prompt(comfy_url, workflow, client_id)
        status.info("Queued in ComfyUI.")
        while True:
            message = ws.recv()
            if isinstance(message, bytes):
                # ComfyUI preview frames begin with two 4-byte big-endian values.
                if len(message) > 8:
                    try:
                        show_preview(preview, message[8:], preprocessor)
                    except OSError:
                        pass
                continue

            event = json.loads(message)
            data = event.get("data", {})
            if event.get("type") == "progress" and data.get("prompt_id") == prompt_id:
                value, maximum = data.get("value", 0), data.get("max", 1)
                progress.progress(min(value / max(maximum, 1), 1.0), text=f"Generating image: step {value} of {maximum}")
            elif event.get("type") == "executed" and data.get("prompt_id") == prompt_id and data.get("node") == "57":
                # PreviewImage also writes a temporary image which is reliable when a
                # browser/WebSocket proxy omits binary preview frames.
                images = data.get("output", {}).get("images", [])
                if images:
                    try:
                        show_preview(preview, get_image(comfy_url, images[0]), preprocessor)
                        status.info("Preprocessor preview is ready. Generating the final image.")
                    except requests.RequestException:
                        pass
            elif event.get("type") == "executing" and data.get("prompt_id") == prompt_id:
                node = data.get("node")
                if node == "56":
                    status.info("Creating the selected preprocessor preview.")
                elif node == "57":
                    status.info("Preprocessor preview is ready. Generating the final image.")
                elif node is None:
                    break
    finally:
        ws.close()

    history = requests.get(api_url(comfy_url, f"/history/{prompt_id}"), timeout=30)
    history.raise_for_status()
    outputs = history.json().get(prompt_id, {}).get("outputs", {}).get("9", {}).get("images", [])
    if not outputs:
        raise RuntimeError("ComfyUI completed without returning a saved image.")
    return get_image(comfy_url, outputs[0])


st.set_page_config(page_title="Turbo ControlNet Engine", layout="wide", initial_sidebar_state="collapsed")
st.markdown(
    """
    <style>
        .stApp { background: #0c1019; color: #f1f5f9; }
        .block-container { max-width: 1280px; padding-top: 3rem; padding-bottom: 3rem; }
        .hero { margin: 0 0 2.4rem; }
        .eyebrow { color: #a78bfa; font-size: .72rem; font-weight: 700; letter-spacing: .16em; margin: 0 0 .45rem; }
        .hero h1 { color: #f8fafc; font-size: clamp(2.5rem, 5vw, 4.5rem); letter-spacing: -.065em; line-height: .92; margin: 0; }
        .hero h1 span { color: #a78bfa; }
        .hero-copy { color: #94a3b8; font-size: 1.05rem; margin: .85rem 0 0; max-width: 38rem; }
        .panel-label { color: #c4b5fd; font-size: .75rem; font-weight: 700; letter-spacing: .12em; margin-bottom: .35rem; }
        .stTextArea textarea, .stTextInput input { background: #121927 !important; border: 1px solid #29354a !important; border-radius: 12px !important; color: #f8fafc !important; }
        .stSelectbox div[data-baseweb="select"] > div { background: #121927 !important; border-color: #29354a !important; border-radius: 12px !important; }
        .stButton > button { background: linear-gradient(135deg, #8b5cf6, #6366f1) !important; border: 0 !important; border-radius: 12px !important; box-shadow: 0 10px 24px rgba(99, 102, 241, .25); font-weight: 700; min-height: 3rem; }
        .stButton > button:hover { background: linear-gradient(135deg, #a78bfa, #7c83ff) !important; }
        [data-testid="stFileUploader"] { background: #121927; border: 1px dashed #46556e; border-radius: 14px; padding: .6rem; }
        [data-testid="stVerticalBlockBorderWrapper"] { background: linear-gradient(145deg, rgba(22, 30, 45, .93), rgba(14, 20, 32, .96)); border: 1px solid #26344b; border-radius: 18px; }
        [data-testid="stSidebar"] { background: #101622; }
    </style>
    <section class="hero">
        <p class="eyebrow">LOCAL COMFYUI IMAGE LAB</p>
        <h1>Turbo <span>ControlNet Engine</span></h1>
        <p class="hero-copy">Turn a reference image and a creative direction into a controlled, downloadable generation.</p>
    </section>
    """,
    unsafe_allow_html=True,
)

with st.sidebar:
    st.header("Connection")
    comfy_url = st.text_input("ComfyUI URL", DEFAULT_COMFY_URL, help="Your local ComfyUI server URL.")

input_column, control_column = st.columns((1, 1.15), gap="large")
with input_column:
    with st.container(border=True):
        st.markdown('<p class="panel-label">01 / REFERENCE</p>', unsafe_allow_html=True)
        uploaded_file = st.file_uploader("Upload an image", type=["png", "jpg", "jpeg", "webp"], label_visibility="collapsed")
        if uploaded_file:
            st.image(uploaded_file, caption="Reference image", use_container_width=True)
        else:
            st.caption("PNG, JPG, JPEG, or WEBP")

with control_column:
    with st.container(border=True):
        st.markdown('<p class="panel-label">02 / CREATIVE DIRECTION</p>', unsafe_allow_html=True)
        prompt = st.text_area("Prompt", height=150, placeholder="Describe the image you want to generate.", label_visibility="collapsed")
        st.markdown('<p class="panel-label">03 / STRUCTURE GUIDE</p>', unsafe_allow_html=True)
        preprocessor = st.selectbox("AIO Aux Preprocessor", PREPROCESSORS, label_visibility="collapsed")
        st.caption(PREPROCESSOR_HELP[preprocessor])
        generate = st.button("Generate image", type="primary", use_container_width=True, disabled=not uploaded_file or not prompt.strip())

st.markdown("<br>", unsafe_allow_html=True)
preview_column, result_column = st.columns(2, gap="large")
with preview_column:
    with st.container(border=True):
        st.markdown('<p class="panel-label">LIVE / PREPROCESSOR OUTPUT</p>', unsafe_allow_html=True)
        preview = st.empty()
        preview.caption("Your selected structure guide will appear here before sampling begins.")
with result_column:
    with st.container(border=True):
        st.markdown('<p class="panel-label">FINAL / GENERATED IMAGE</p>', unsafe_allow_html=True)
        result = st.empty()
        result.caption("Your final image will appear here when generation completes.")

if generate:
    status = st.empty()
    progress = st.progress(0, text="Preparing upload...")
    try:
        run_id = uuid.uuid4().hex[:12]
        suffix = Path(uploaded_file.name).suffix.lower() or ".png"
        image_name = upload_image(comfy_url, uploaded_file, f"turbo_{run_id}{suffix}")
        workflow = build_workflow(image_name, prompt.strip(), preprocessor, f"turbo/{run_id}")
        final_image = run_generation(comfy_url, workflow, preprocessor, status, progress, preview)
        progress.progress(1.0, text="Complete")
        status.success("Image generated.")
        result.image(final_image, caption="Generated image", use_container_width=True)
        st.download_button("Download generated image", final_image, file_name=f"turbo_{run_id}.png", mime="image/png")
    except (requests.RequestException, websocket.WebSocketException, RuntimeError, KeyError) as error:
        status.error(f"Generation failed: {error}")
