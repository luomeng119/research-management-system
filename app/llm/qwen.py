# -*- coding: utf-8 -*-
"""
Qwen 通用意图理解模型封装
提供智能摘要、意图检索理解、内容生成能力
"""
import logging
from app.llm.pool import ModelPool

logger = logging.getLogger(__name__)

# ============ Prompt 模板 ============

SUMMARIZE_PROMPT = """请为以下科研文档生成一段简洁的摘要（{max_length}字以内），概括主要内容和成果：

{content}

摘要："""

SEARCH_UNDERSTAND_PROMPT = """用户想找："{query}"
以下是一条科研项目/设备/标准的描述，请判断是否与用户需求相关。

标题：{title}
描述：{description}

请只回答"是"或"否"，不要解释："""

GENERATE_PROMPT = """你是一个科研文档助手。请根据以下信息，生成一段{genre}：

{context}

要求：
1. 语言正式、专业，符合科研文档规范
2. 内容真实准确，不要虚构数据
3. 字数控制在{length}字以内

{genre}："""


class QwenHelper:
    """Qwen 通用意图理解助手"""

    def __init__(self, pool: ModelPool = None):
        self.pool = pool or ModelPool()

    def _call(self, prompt: str, max_tokens: int = 256, temperature: float = 0.3) -> str:
        """通用调用"""
        try:
            model = self.pool.get_model('qwen')
            if model is None:
                raise RuntimeError("Qwen 模型未加载")

            output = model(
                prompt,
                max_tokens=max_tokens,
                temperature=temperature,
                stop=["<|end_of_text|>", "摘要：", "是或否：", "回答："],
                echo=False,
            )
            return output['choices'][0]['text'].strip()
        except Exception as e:
            logger.error(f"[QwenHelper] 调用失败: {e}")
            raise

    # ============ 能力 1：智能摘要 ============

    def summarize(self, content: str, max_length: int = 100) -> dict:
        """
        对文档内容生成摘要。

        返回:
            {"summary": str, "truncated": bool}
        """
        if not content or not content.strip():
            return {"summary": "", "truncated": False}

        # 截断过长内容（Qwen 1.5B 上下文有限）
        MAX_INPUT = 1500
        truncated = False
        if len(content) > MAX_INPUT:
            content = content[:MAX_INPUT]
            truncated = True

        prompt = SUMMARIZE_PROMPT.format(content=content, max_length=max_length)

        try:
            summary = self._call(prompt, max_tokens=max_length + 50, temperature=0.3)
            return {"summary": summary, "truncated": truncated}
        except Exception as e:
            logger.error(f"[QwenHelper] 摘要生成失败: {e}")
            return {"summary": "", "truncated": truncated, "error": str(e)}

    # ============ 能力 2：意图匹配判断 ============

    def is_relevant(self, query: str, title: str, description: str = "") -> bool:
        """
        判断单条记录是否与用户查询意图相关。

        参数:
            query: 用户自然语言查询（如"涉及密码算法的项目"）
            title: 记录标题
            description: 记录描述（可选）

        返回:
            True = 相关，False = 不相关
        """
        if not query or not title:
            return False

        # 空描述降级处理
        desc = description.strip() if description else "无描述"
        prompt = SEARCH_UNDERSTAND_PROMPT.format(
            query=query,
            title=title,
            description=desc,
        )

        try:
            answer = self._call(prompt, max_tokens=10, temperature=0.0)
            return "是" in answer[:5]
        except Exception:
            # 出错时保守返回不相关
            return False

    def filter_relevant(self, query: str, records: list) -> list:
        """
        从记录列表中筛选出与 query 意图相关的记录。

        参数:
            query: 自然语言查询
            records: [{"title": str, "description": str, ...}, ...]

        返回:
            仅包含相关记录的列表（保留原始完整 dict）
        """
        if not query or not records:
            return records  # 空查询直接返回原列表

        relevant = []
        for r in records:
            title = r.get('title') or r.get('name') or ""
            desc = r.get('description') or r.get('main_purpose') or r.get('tech_index') or ""
            if self.is_relevant(query, title, desc):
                relevant.append(r)
        return relevant

    # ============ 能力 3：内容生成 ============

    def generate(self, genre: str, context: str, length: int = 300) -> dict:
        """
        根据给定信息生成科研文档。

        参数:
            genre: 文体类型，如"项目申报摘要"、"邮件正文"、"评审意见"
            context: 背景信息
            length: 最大字数

        返回:
            {"content": str}
        """
        if not context:
            return {"content": "", "error": "缺少背景信息"}

        prompt = GENERATE_PROMPT.format(
            genre=genre,
            context=context,
            length=length,
        )

        try:
            content = self._call(prompt, max_tokens=length + 100, temperature=0.5)
            return {"content": content}
        except Exception as e:
            logger.error(f"[QwenHelper] 内容生成失败: {e}")
            return {"content": "", "error": str(e)}
