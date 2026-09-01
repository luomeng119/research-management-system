# -*- coding: utf-8 -*-
"""
中文文本纠错模型封装
基于 chinese-text-correction GGUF 模型
"""
import re
import logging
import urllib.request
import urllib.error
import json
from app.llm.pool import ModelPool
from config import ENABLE_LLM

logger = logging.getLogger(__name__)

# HTTP 推理服务器地址
INFERENCE_URL = "http://127.0.0.1:18789/complete"

# Prompt 模板
SYSTEM_PROMPT = """你是一个中文文本校对专家。请检查以下文本中的错别字、标点错误和格式问题，并返回纠错后的文本。

要求：
1. 只修正明确的错误，不要改变原文的表达风格
2. 标点符号使用中文全角符号（，。：；？！""）而非英文符号
3. 如果没有问题，直接返回原文，不添加任何解释
4. 只返回纠错后的文本，不要有任何前缀说明

原文："""

SUMMARY_PROMPT = """你是一个中文文本校对专家。请检查以下文本中的错别字、标点错误和格式问题。
同时列出所有错误，格式为：[位置] 原文→纠错 原因
如果无错误，回复"无错误"。

原文：{text}

纠错结果："""

MAX_INPUT_LENGTH = 2000  # 单次纠错最大输入字符数


class Corrector:
    """中文文本纠错器"""

    def __init__(self, pool: ModelPool = None):
        self.pool = pool or ModelPool()

    def correct(self, text: str, max_length: int = MAX_INPUT_LENGTH) -> dict:
        """
        对输入文本进行纠错。

        参数:
            text: 待纠错文本
            max_length: 最大输入长度（超出则截断）

        返回:
            {
                "corrected": str,   # 纠错后文本
                "errors": List[dict],  # 错误列表（供前端展示）
                "truncated": bool,    # 是否被截断
            }
        """
        if not text or not text.strip():
            return {"corrected": "", "errors": [], "truncated": False}

        truncated = False
        if len(text) > max_length:
            text = text[:max_length]
            truncated = True

        # 构建 prompt
        prompt = SYSTEM_PROMPT + text + "\n\n纠错后："

        if not ENABLE_LLM:
            return {"corrected": text, "errors": [], "truncated": truncated, "error": "本地推理未启用，请在 config.py 中设置 ENABLE_LLM = True 开启"}

        try:
            # 优先使用 HTTP 推理服务器（Node.js）
            corrected = self._http_complete(prompt, max_tokens=512, temperature=0.1)

            # 去除可能的引号包裹
            corrected = corrected.strip('""「」『』')

            return {
                "corrected": corrected,
                "errors": [],  # 简化版不返回详细错误位置
                "truncated": truncated,
            }

        except Exception as e:
            logger.error(f"[Corrector] 纠错失败: {e}")
            return {"corrected": text, "errors": [], "truncated": truncated, "error": str(e)}

    def correct_with_details(self, text: str) -> dict:
        """
        带详细错误列表的纠错（调用两次模型，成本较高）。
        """
        if not ENABLE_LLM:
            return {"corrected": text, "errors": [], "truncated": False, "error": "本地推理未启用，请在 config.py 中设置 ENABLE_LLM = True 开启"}

        if not text or not text.strip():
            return {"corrected": "", "errors": [], "truncated": False}

        truncated = False
        if len(text) > MAX_INPUT_LENGTH:
            text = text[:MAX_INPUT_LENGTH]
            truncated = True

        prompt = SUMMARY_PROMPT.format(text=text)

        try:
            # 带详细错误的版本，调用两次
            raw = self._http_complete(prompt, max_tokens=1024, temperature=0.1)

            # 解析错误列表（简单正则）
            errors = self._parse_errors(raw, text)

            # 再次调用获取纯纠错文本
            prompt2 = SYSTEM_PROMPT + text + "\n\n纠错后："
            corrected = self._http_complete(prompt2, max_tokens=512, temperature=0.1).strip('""「」『』')

            return {
                "corrected": corrected,
                "errors": errors,
                "truncated": truncated,
            }

        except Exception as e:
            logger.error(f"[Corrector] 详细纠错失败: {e}")
            return {"corrected": text, "errors": [], "truncated": truncated, "error": str(e)}

    def _parse_errors(self, raw: str, original: str) -> list:
        """从模型输出中解析错误列表"""
        errors = []
        # 格式：[位置] 原文→纠错 原因
        pattern = re.compile(r'\[(\d+)\]\s*(.{1,20})→(.{1,20})\s*(.*)')
        for match in pattern.finditer(raw):
            pos = int(match.group(1))
            old = match.group(2).strip()
            new = match.group(3).strip()
            reason = match.group(4).strip() if len(match.groups()) > 3 else '表达不当'
            errors.append({"pos": pos, "old": old, "new": new, "reason": reason})
        return errors

    def _http_complete(self, prompt: str, max_tokens: int = 256, temperature: float = 0.1) -> str:
        """通过 HTTP 调用 Node.js 推理服务器"""
        payload = json.dumps({
            "prompt": prompt,
            "max_tokens": max_tokens,
            "temperature": temperature,
        }).encode('utf-8')

        req = urllib.request.Request(
            INFERENCE_URL,
            data=payload,
            headers={'Content-Type': 'application/json'},
            method='POST'
        )

        try:
            with urllib.request.urlopen(req, timeout=120) as resp:
                data = json.loads(resp.read().decode('utf-8'))
                return data['choices'][0]['text']
        except urllib.error.URLError as e:
            logger.error(f"[Corrector] HTTP 请求失败: {e}")
            raise Exception(f"推理服务器连接失败: {e}")
        except (KeyError, json.JSONDecodeError) as e:
            logger.error(f"[Corrector] 推理响应格式错误: {e}")
            raise Exception(f"推理响应格式错误: {e}")
