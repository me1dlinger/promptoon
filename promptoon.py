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
from cryptography.fernet import Fernet
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
PROXY_URL = "http://127.0.0.1:7890"
os.environ["HTTP_PROXY"] = PROXY_URL
os.environ["HTTPS_PROXY"] = PROXY_URL
os.environ["ALL_PROXY"] = PROXY_URL

ENCRYPTION_KEY = b"zDqHdcnVYuuo6RLCfm7LZ-RQHBPHtW3P9B9JII4GjwM="
cipher_suite = Fernet(ENCRYPTION_KEY)

# 设置requests的默认代理
proxies = {"http": PROXY_URL, "https": PROXY_URL}

logger.info(f"✅ 已设置代理: {PROXY_URL}")


def encrypt_api_key(api_key):
    """加密API Key"""
    return cipher_suite.encrypt(api_key.encode()).decode()


def decrypt_api_key(encrypted_api_key):
    """解密API Key"""
    return cipher_suite.decrypt(encrypted_api_key.encode()).decode()


# 我们不再使用固定的API KEY，而是从前端传递

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


def call_gemini_api(
    image_base64, api_key, model_version, save_dir, unique_filename, file_uuid
):
    """调用Gemini API生成提示词"""
    try:
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
                            "mime_type": "image/jpeg",
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

        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model_version}:generateContent?key={api_key}"

        proxies = {
            "http": os.environ.get("HTTP_PROXY", ""),
            "https": os.environ.get("HTTPS_PROXY", ""),
        }

        # 检查代理连通性
        if not check_proxy_connectivity(
            os.environ.get("HTTPS_PROXY") or os.environ.get("HTTP_PROXY")
        ):
            return jsonify({"success": False, "error": "代理服务器无法连接"}), 500

        logger.info(f"开始请求 Gemini API ({model_version})...")
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
                        "error": f"Gemini API错误: {response.status_code}",
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


def call_doubao_api(
    image_base64, api_key, model_version, save_dir, unique_filename, file_uuid
):
    """调用豆包API生成提示词"""
    try:
        # TODO: 需要实现豆包API的实际调用逻辑
        # 目前返回固定字符串以满足接口

        # 模拟API调用
        import time

        time.sleep(2)

        # 读取示例响应
        example_response = {
            "english_prompt": {
                "style_medium": "Digital anime illustration",
                "style_details": "Clean linework, cel-shading, soft color gradients, large expressive glossy eyes",
                "scene": "A young anime girl_character in a playful pose",
                "subject": "Girl with blue hair in twin pigtails, blue eyes, blushing cheeks",
                "outfit_props": "White shirt with blue bows, blue pleated skirt, thigh-high socks",
                "background": "Soft, out-of-focus light-colored background",
                "composition": "High-angle shot, centered composition",
                "lighting_color": "Bright and airy, cool-toned palette",
                "special_effects": "Subtle sweat drop graphic",
                "avoid": "realistic, 3D, dark colors, complex background",
            },
            "chinese_prompt": {
                "style_medium": "数字动漫插画",
                "style_details": "清晰的线稿,赛璐璐着色,柔和的色彩渐变,大而富有表现力的光泽眼睛",
                "scene": "一位年轻的动漫女孩以俏皮的姿态出镜",
                "subject": "蓝色双马尾少女,蓝色眼睛,脸颊泛红",
                "outfit_props": "白色衬衫配蓝色蝴蝶结,蓝色百褶裙,过膝袜",
                "background": "柔和失焦的浅色调背景",
                "composition": "俯视角度,居中构图",
                "lighting_color": "明亮通透,冷色调",
                "special_effects": "细微的汗滴图形",
                "avoid": "写实风格,3D渲染,深色调,复杂背景",
            },
            "full_prompt_en": "Digital anime illustration, clean linework, cel-shading, soft color gradients, large expressive glossy eyes, young anime girl_character in playful pose, blue twin pigtails, blue eyes, blushing cheeks, white shirt with blue bows, blue pleated skirt, thigh-high socks, soft out-of-focus light-colored background, high-angle shot, centered composition, bright and airy lighting, cool-toned palette, subtle sweat drop graphic",
            "full_prompt_cn": "数字动漫插画风格,清晰线稿和赛璐璐着色,柔和色彩渐变,大而富有表现力的光泽眼睛。画面中是一位年轻的动漫女孩以俏皮姿态出镜,蓝色双马尾发型,蓝色眼睛,脸颊泛红。她身穿白色衬衫配有蓝色蝴蝶结,蓝色百褶裙和过膝袜。背景为柔和失焦的浅色调。采用俯视角度和居中构图,光线明亮通透呈冷色调,带有细微的汗滴图形特效。",
        }

        try:
            detail_path = os.path.join(
                save_dir, f"{os.path.splitext(unique_filename)[0]}.json"
            )
            detail_content = {
                "ip": get_real_ip(),
                "prompt_data": example_response,
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
                "prompt_data": example_response,
                "raw_response": json.dumps(example_response, ensure_ascii=False),
                "uuid": file_uuid,
            }
        )

    except Exception as e:
        logger.exception("处理图片失败: %s", e)
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/encrypt_api_key", methods=["POST"])
def encrypt_api_key_route():
    try:
        data = request.get_json()
        api_key = data.get("api_key")

        if not api_key:
            return jsonify({"success": False, "error": "API Key不能为空"}), 400

        # 加密API Key
        encrypted_key = encrypt_api_key(api_key)
        return jsonify({"success": True, "encrypted_key": encrypted_key})
    except Exception as e:
        logger.exception("加密API Key失败: %s", e)
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/generate_prompt", methods=["POST"])
def generate_prompt():
    try:
        if "image" not in request.files:
            return jsonify({"success": False, "error": "没有上传图片"}), 400

        file = request.files["image"]
        if file.filename == "":
            return jsonify({"success": False, "error": "没有选择文件"}), 400
        # 获取API配置参数
        api_model = request.form.get("api_model", "gemini")
        model_version = request.form.get("model_version", "gemini-2.5-flash-lite")
        encrypted_api_key = request.form.get("api_key")

        if not encrypted_api_key:
            return jsonify({"success": False, "error": "API Key不能为空"}), 400
        try:
            api_key = decrypt_api_key(encrypted_api_key)
            print(api_key)
        except Exception as e:
            logger.error(f"API Key解密失败: {e}")
            return jsonify({"success": False, "error": "API Key解密失败"}), 4000

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

        # 根据选择的模型调用相应的API
        if api_model == "gemini":
            return call_gemini_api(
                image_base64,
                api_key,
                model_version,
                save_dir,
                unique_filename,
                file_uuid,
            )
        elif api_model == "doubao":
            return call_doubao_api(
                image_base64,
                api_key,
                model_version,
                save_dir,
                unique_filename,
                file_uuid,
            )
        else:
            return jsonify({"success": False, "error": "不支持的AI模型"}), 400

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

    logger.info("✅ 网络检查通过,准备启动 Flask 服务...")
    logger.info("请确保依赖已安装:pip install flask flask-cors pillow requests")

    app.run(debug=True, host="0.0.0.0", port=5000)
