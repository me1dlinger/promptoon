import base64
import io
import json
import logging
import os
import socket
import uuid
from datetime import datetime
from logging.handlers import TimedRotatingFileHandler
from urllib.parse import urlparse

import requests
from flask import Flask, jsonify, render_template, request
from flask_cors import CORS
from PIL import Image

# 日志目录
LOG_DIR = "logs"
os.makedirs(LOG_DIR, exist_ok=True)

# 创建日志器
logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

# 日志格式
formatter = logging.Formatter("%(asctime)s - %(levelname)s - %(message)s")

# 控制台 Handler
console_handler = logging.StreamHandler()
console_handler.setFormatter(formatter)
logger.addHandler(console_handler)

# 每日轮转文件 Handler
file_handler = TimedRotatingFileHandler(
    filename=os.path.join(LOG_DIR, "server.log"),
    when="midnight",
    backupCount=7,
    encoding="utf-8",
    delay=True,
)
file_handler.setFormatter(formatter)
logger.addHandler(file_handler)

app = Flask(__name__)
CORS(app)
PROXY_URL = "http://host:port"
os.environ["HTTP_PROXY"] = PROXY_URL
os.environ["HTTPS_PROXY"] = PROXY_URL
os.environ["ALL_PROXY"] = PROXY_URL

# 设置requests的默认代理
proxies = {"http": PROXY_URL, "https": PROXY_URL}

logger.info(f"✅ 已设置代理: {PROXY_URL}")

# 读取 API KEY
GEMINI_API_KEY = "GEMINI_API_KEY"
if not GEMINI_API_KEY:
    logger.error("❌ 环境变量 GEMINI_API_KEY 未设置,程序即将退出。")
    exit(1)

UPLOAD_BASE_DIR = "./uploads"
CONFIG_DIR = "./prompts"


def load_prompt():
    """从本地文件加载提示词配置"""
    try:
        with open(
            os.path.join(CONFIG_DIR, "default_prompt.txt"), "r", encoding="utf-8"
        ) as f:
            prompt = f.read()
        logger.info("✅ 成功加载提示词配置文件")
        return prompt
    except FileNotFoundError as e:
        logger.error(f"❌ 提示词配置文件未找到: {e}")
        return "默认提示词未配置"


def load_imitation_dialogs():
    """从本地文件加载示例对话配置"""
    try:
        with open(
            os.path.join(CONFIG_DIR, "default_dialogs.json"), "r", encoding="utf-8"
        ) as f:
            dialogs = json.load(f)
        logger.info("✅ 成功加载示例对话配置文件")
        return dialogs
    except (FileNotFoundError, json.JSONDecodeError) as e:
        logger.error(f"❌ 示例对话配置文件加载失败: {e}")
        return [
            {"role": "user", "parts": [{"text": "[未配置]"}]},
            {"role": "model", "parts": [{"text": "示例对话未配置"}]},
        ]


# 初始化时加载配置
SYSTEM_PROMPT = load_prompt()
IMITATION_DIALOGS = load_imitation_dialogs()


def compress_image(image_data, max_size_mb=1):
    try:
        img = Image.open(io.BytesIO(image_data))
        if img.mode != "RGB":
            img = img.convert("RGB")

        original_size = len(image_data)
        max_size_bytes = max_size_mb * 1024 * 1024

        if original_size <= max_size_bytes:
            return image_data

        quality = 85
        while True:
            buffer = io.BytesIO()
            img.save(buffer, format="JPEG", quality=quality, optimize=True)
            compressed_data = buffer.getvalue()
            if len(compressed_data) <= max_size_bytes or quality <= 10:
                return compressed_data
            quality -= 10

    except Exception as e:
        logger.exception("图片压缩失败: %s", e)
        return image_data


def parse_prompt_response(response_text):
    """解析AI返回的结构化提示词"""
    try:
        # 尝试解析JSON格式
        data = json.loads(response_text)
        return data
    except json.JSONDecodeError:
        logger.warning("无法解析为JSON,返回原始文本")
        return {"raw_response": response_text}


def extract_token_usage(usage_metadata):
    """解析 Gemini usageMetadata"""

    def to_dict(details):
        return {d["modality"].lower(): d["tokenCount"] for d in details}

    return {
        "prompt_tokens": usage_metadata.get("promptTokenCount", 0),
        "completion_tokens": usage_metadata.get("candidatesTokenCount", 0),
        "total_tokens": usage_metadata.get("totalTokenCount", 0),
        "prompt_detail": to_dict(usage_metadata.get("promptTokensDetails", [])),
        "completion_detail": to_dict(usage_metadata.get("candidatesTokensDetails", [])),
    }


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/generate_prompt", methods=["POST"])
def generate_prompt():
    try:
        if "image" not in request.files:
            return jsonify({"success": False, "error": "没有上传图片"}), 400

        file = request.files["image"]
        if file.filename == "":
            return jsonify({"success": False, "error": "没有选择文件"}), 400

        image_data = file.read()
        today_str = datetime.now().strftime("%Y-%m-%d")
        save_dir = os.path.join(UPLOAD_BASE_DIR, today_str)
        os.makedirs(save_dir, exist_ok=True)
        file_uuid = uuid.uuid4().hex
        ext = os.path.splitext(file.filename)[-1] or ".jpg"
        unique_filename = f"{file_uuid}{ext}"
        save_path = os.path.join(save_dir, unique_filename)

        with open(save_path, "wb") as f:
            f.write(image_data)

        if len(image_data) > 3 * 1024 * 1024:
            logger.info("图片超过3MB,开始压缩...")
            image_data = compress_image(image_data, max_size_mb=0.5)
            logger.info(f"压缩后图片大小: {len(image_data) / 1024 / 1024:.2f}MB")

        image_base64 = base64.b64encode(image_data).decode("utf-8")

        contents = [
            {"role": "user", "parts": [{"text": SYSTEM_PROMPT}]},
            {
                "role": "model",
                "parts": [
                    {"text": "我明白了,我会按照您的要求分析图片并生成结构化的提示词..."}
                ],
            },
        ]
        contents.extend(IMITATION_DIALOGS)
        contents.append(
            {
                "role": "user",
                "parts": [
                    {"text": "请分析这张二次元图片并生成提示词:"},
                    {
                        "inline_data": {
                            "mime_type": f"image/{file.content_type.split('/')[-1]}",
                            "data": image_base64,
                        }
                    },
                ],
            }
        )

        payload = {
            "contents": contents,
            "generationConfig": {"maxOutputTokens": 2048, "temperature": 0.7},
        }

        url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash-lite:generateContent?key={GEMINI_API_KEY}"

        proxies = {
            "http": os.environ.get("HTTP_PROXY", ""),
            "https": os.environ.get("HTTPS_PROXY", ""),
        }

        logger.info("开始请求 Gemini API...")
        response = requests.post(
            url,
            json=payload,
            headers={"Content-Type": "application/json"},
            proxies=proxies,
            timeout=60,
        )

        if response.status_code != 200:
            logger.error(
                "Gemini API 返回错误: %s - %s", response.status_code, response.text
            )
            return (
                jsonify(
                    {
                        "success": False,
                        "error": f"错误: {response.status_code}",
                        "details": response.text,
                    }
                ),
                500,
            )

        response_data = response.json()
        try:
            response_text = response_data["candidates"][0]["content"]["parts"][0][
                "text"
            ]
        except (KeyError, IndexError) as e:
            logger.exception("解析响应失败: %s", response_data)
            return (
                jsonify(
                    {
                        "success": False,
                        "error": "无法解析模型响应",
                        "raw_response": response_data,
                    }
                ),
                500,
            )

        usage_metadata = response_data.get("usageMetadata", {})
        token_usage = extract_token_usage(usage_metadata)

        # 解析结构化响应
        parsed_data = parse_prompt_response(response_text)

        try:
            detail_path = os.path.join(
                save_dir, f"{os.path.splitext(unique_filename)[0]}.json"
            )
            detail_content = {
                "ip": get_real_ip(),
                "prompt_data": parsed_data,
                "token_usage": token_usage,
                "timestamp": datetime.now().isoformat(),
            }
            with open(detail_path, "w", encoding="utf-8") as f:
                json.dump(detail_content, f, ensure_ascii=False, indent=2)
            logger.info(f"✅ 成功保存提示词详情到 {detail_path}")
        except Exception as e:
            logger.warning("⚠️ 保存提示词详情失败: %s", e)

        return jsonify(
            {
                "success": True,
                "prompt_data": parsed_data,
                "raw_response": response_text,
                "uuid": file_uuid,
            }
        )

    except Exception as e:
        logger.exception("处理图片失败: %s", e)
        return jsonify({"success": False, "error": str(e)}), 500


def get_real_ip():
    if "X-Forwarded-For" in request.headers:
        ip_list = request.headers["X-Forwarded-For"].split(",")
        return ip_list[0].strip()
    return request.remote_addr


def check_proxy_connectivity(proxy_url, test_host="www.google.com"):
    try:
        parsed = urlparse(proxy_url)
        host = parsed.hostname
        port = parsed.port
        logger.info(f"检测代理可达性:{host}:{port}...")
        with socket.create_connection((host, port), timeout=5):
            logger.info("✅ 代理端口可达")
            return True
    except Exception as e:
        logger.error("❌ 代理端口连接失败: %s", e)
        return False


def check_gemini_access():
    try:
        proxies = {"http": os.environ["HTTP_PROXY"], "https": os.environ["HTTPS_PROXY"]}
        response = requests.get(
            "https://generativelanguage.googleapis.com/v1/models/gemini-2.5-flash-lite",
            params={"key": GEMINI_API_KEY},
            proxies=proxies,
            timeout=10,
        )
        if response.status_code == 200 or response.status_code == 401:
            return True
        logger.error("Gemini API 访问错误: HTTP %s", response.status_code)
        return False
    except Exception as e:
        logger.error("Gemini API 访问失败: %s", e)
        return False


def check_internet_via_proxy(proxy_url):
    try:
        logger.info("验证代理是否能访问外网 (Google)...")
        proxies = {"http": proxy_url, "https": proxy_url}
        response = requests.get("https://www.google.com", proxies=proxies, timeout=10)
        if response.status_code == 200:
            logger.info("✅ 能通过代理成功访问 Google")
            return True
        else:
            logger.warning("❌ Google 返回非200状态码: %s", response.status_code)
            return False
    except Exception as e:
        logger.error("❌ 无法通过代理访问 Google: %s", e)
        return False


if __name__ == "__main__":
    logger.info("初始化环境检查...")

    proxy = os.environ.get("HTTPS_PROXY") or os.environ.get("HTTP_PROXY")

    if not check_proxy_connectivity(proxy):
        logger.error("❌ 代理端口无法连接,程序即将退出。")
        exit(1)

    if not check_internet_via_proxy(proxy):
        logger.error("❌ 无法通过代理访问外网,程序即将退出。")
        exit(1)

    if not check_gemini_access():
        logger.error("❌ 无法访问Gemini API,请检查代理设置或API密钥")
        exit(1)

    logger.info("✅ 所有网络检查通过,准备启动 Flask 服务...")
    logger.info("请确保依赖已安装:pip install flask flask-cors pillow requests")

    app.run(debug=True, host="0.0.0.0", port=5000)
