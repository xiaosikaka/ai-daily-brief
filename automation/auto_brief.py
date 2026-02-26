#!/usr/bin/env python3
"""
AI 日报自动生成 & 推送脚本

功能：
1. 调用 LLM API (OpenAI / Anthropic / DeepSeek 等) 搜索并生成 AI 简报
2. 生成 HTML 日报文件
3. 推送摘要到企业微信群机器人 / 个人微信（通过 Server酱 / PushPlus）

使用：
  python3 auto_brief.py                    # 生成并推送
  python3 auto_brief.py --no-push          # 仅生成不推送
  python3 auto_brief.py --push-only        # 仅推送最新一期

定时任务 (crontab -e)：
  0 9 * * * cd /path/to/ai-daily-brief && /usr/bin/python3 automation/auto_brief.py >> automation/logs/cron.log 2>&1
"""

import os
import sys
import json
import yaml
import argparse
import logging
import subprocess
import requests
from datetime import datetime, timedelta
from pathlib import Path
from string import Template
from urllib.parse import quote

# ============================================================
# 配置加载
# ============================================================

SCRIPT_DIR = Path(__file__).parent
PROJECT_ROOT = SCRIPT_DIR.parent
CONFIG_PATH = SCRIPT_DIR / "config.yaml"
TEMPLATE_PATH = PROJECT_ROOT / "assets" / "template.html"
OUTPUT_DIR = PROJECT_ROOT / "AI简报"
LOG_DIR = SCRIPT_DIR / "logs"

LOG_DIR.mkdir(exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(LOG_DIR / "auto_brief.log", encoding="utf-8"),
        logging.StreamHandler(),
    ],
)
logger = logging.getLogger(__name__)


def load_config() -> dict:
    """加载配置文件"""
    if not CONFIG_PATH.exists():
        logger.error(f"配置文件不存在: {CONFIG_PATH}")
        logger.error("请复制 config.yaml.example 为 config.yaml 并填写你的 API Key")
        sys.exit(1)
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


# ============================================================
# LLM API 调用 — 生成简报内容
# ============================================================

def search_and_generate_brief(config: dict, date_start: str, date_end: str) -> dict:
    """
    调用 LLM API 搜索 AI 新闻并生成结构化简报。

    支持的 LLM 提供商：
    - openai (GPT-4o / GPT-4o-mini，需搭配搜索插件或先用搜索 API)
    - anthropic (Claude)
    - deepseek (DeepSeek，支持联网搜索)
    - zhipu (智谱 GLM，支持联网搜索)

    返回格式：
    {
        "highlights": {"index": 1, "title": "..."},
        "items": [
            {
                "index": 1,
                "title": "...",
                "date": "2026-02-25",
                "content": "...",
                "value": "...",
                "tags": ["#技术", "#产品"],
                "link": "https://...",
                "category": "技术突破",
                "is_external_source": true
            }
        ]
    }
    """
    llm_config = config.get("llm", {})
    provider = llm_config.get("provider", "openai")
    api_key = llm_config.get("api_key", "")
    model = llm_config.get("model", "gpt-4o")
    base_url = llm_config.get("base_url", "")

    if not api_key:
        logger.error("LLM API Key 未配置")
        sys.exit(1)

    # 加载信息源列表
    info_sources_path = PROJECT_ROOT / "references" / "info-sources.md"
    info_sources = info_sources_path.read_text(encoding="utf-8") if info_sources_path.exists() else ""

    system_prompt = f"""你是一个 AI 日报助手。你的任务是搜索并整理 **{date_start} 到 {date_end}** 期间的 AI 领域重大新闻。

## 固定信息源列表
{info_sources}

## 严格的时效性规则（最重要）

1. **只收录确认在 {date_start} 至 {date_end} 之间实际发布/公布的信息**
2. 每条信息必须注明准确的发布日期（YYYY-MM-DD 格式）
3. **验证方法**：信息的发布日期必须可以从以下至少一个途径确认：
   - 官方博客/新闻稿的发布时间戳
   - 社交媒体帖子的发布时间
   - 新闻报道中明确提到的日期
4. **严禁收录以下内容**：
   - 无法确认具体发布日期的信息
   - 发布日期在 {date_start} 之前的旧闻（即使最近才被广泛讨论）
   - 你不确定是否真实存在的信息 —— **绝对不要编造或杜撰任何新闻**
   - 基于你训练数据中的历史知识而非实时搜索结果的信息
5. **宁缺毋滥**：如果搜索到的符合时间范围的高质量信息不足 3 条，就只返回实际找到的条数，不要凑数
6. 每条信息的 link 字段必须是**真实可访问的 URL**，不要编造链接

## 信息筛选标准

- 按三大分类归组：技术突破、产品应用、行业动态
- 每条信息包含：标题、发布日期、核心内容(50-100字)、价值说明、标签(#技术/#产品/#应用/#论文/#行业)、真实链接
- 选出 1 条最具价值的作为"深度学习精选"
- 标记非固定信息源（链接域名不在上述固定信息源列表中的标记为 true）

## 质量过滤

仅保留满足以下条件中**至少 2 条**的内容：
- 来自官方/顶级研究者（优先级最高）
- 有明确技术/产品突破
- 能直接落地/复现
- 影响行业方向

## 输出格式

请以 JSON 格式返回，结构如下：
{{
    "highlight": {{"index": 1, "title": "精选标题"}},
    "items": [
        {{
            "index": 1,
            "title": "标题",
            "date": "YYYY-MM-DD",
            "content": "核心内容50-100字",
            "value": "价值说明",
            "tags": ["#技术"],
            "link": "https://...",
            "category": "技术突破",
            "is_external_source": false
        }}
    ]
}}"""

    user_message = f"请搜索 {date_start} 到 {date_end} 期间实际发布的 AI 领域重大新闻。注意：只收录你能确认在这个日期范围内真实发布的信息，不要包含旧闻或无法确认发布日期的内容。宁可少收录也不要编造。"

    # 根据 provider 调用不同 API
    if provider in ("openai", "deepseek", "zhipu"):
        result = _call_openai_compatible(
            api_key=api_key,
            model=model,
            base_url=base_url or _get_default_base_url(provider),
            system_prompt=system_prompt,
            user_message=user_message,
        )
    elif provider == "anthropic":
        result = _call_anthropic(
            api_key=api_key,
            model=model,
            system_prompt=system_prompt,
            user_message=user_message,
        )
    else:
        logger.error(f"不支持的 LLM 提供商: {provider}")
        sys.exit(1)

    # 解析 JSON
    try:
        # 尝试从返回内容中提取 JSON
        text = result
        if "```json" in text:
            text = text.split("```json")[1].split("```")[0]
        elif "```" in text:
            text = text.split("```")[1].split("```")[0]
        brief_data = json.loads(text.strip())
        return brief_data
    except (json.JSONDecodeError, IndexError) as e:
        logger.error(f"LLM 返回内容解析失败: {e}")
        logger.error(f"原始返回:\n{result[:500]}")
        sys.exit(1)


def _get_default_base_url(provider: str) -> str:
    """获取默认的 API base URL"""
    urls = {
        "openai": "https://api.openai.com/v1",
        "deepseek": "https://api.deepseek.com/v1",
        "zhipu": "https://open.bigmodel.cn/api/paas/v4",
    }
    return urls.get(provider, "https://api.openai.com/v1")


def _call_openai_compatible(api_key: str, model: str, base_url: str,
                            system_prompt: str, user_message: str) -> str:
    """调用 OpenAI 兼容 API (含 DeepSeek, 智谱等)"""
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_message},
        ],
        "temperature": 0.3,
        "max_tokens": 4096,
    }

    url = f"{base_url.rstrip('/')}/chat/completions"
    logger.info(f"调用 LLM API: {url} (model={model})")

    resp = requests.post(url, headers=headers, json=payload, timeout=120)
    resp.raise_for_status()
    data = resp.json()
    return data["choices"][0]["message"]["content"]


def _call_anthropic(api_key: str, model: str, system_prompt: str, user_message: str) -> str:
    """调用 Anthropic Claude API"""
    headers = {
        "x-api-key": api_key,
        "anthropic-version": "2023-06-01",
        "Content-Type": "application/json",
    }
    payload = {
        "model": model,
        "max_tokens": 4096,
        "system": system_prompt,
        "messages": [
            {"role": "user", "content": user_message},
        ],
    }

    url = "https://api.anthropic.com/v1/messages"
    logger.info(f"调用 Anthropic API (model={model})")

    resp = requests.post(url, headers=headers, json=payload, timeout=120)
    resp.raise_for_status()
    data = resp.json()
    return data["content"][0]["text"]


# ============================================================
# HTML 生成
# ============================================================

def generate_html(brief_data: dict, date_start: str, date_end: str) -> str:
    """根据简报数据和模板生成 HTML"""
    template_text = TEMPLATE_PATH.read_text(encoding="utf-8")
    items = brief_data.get("items", [])
    highlight = brief_data.get("highlight", {})

    total_count = len(items)

    # 按分类分组
    categories = {
        "技术突破": [],
        "产品应用": [],
        "行业动态": [],
    }
    for item in items:
        cat = item.get("category", "行业动态")
        if cat in categories:
            categories[cat].append(item)
        else:
            categories["行业动态"].append(item)

    # 生成卡片 HTML
    cards_html = ""

    section_config = {
        "技术突破": {"icon": "🔬", "class": "技术突破"},
        "产品应用": {"icon": "🚀", "class": "产品应用"},
        "行业动态": {"icon": "📊", "class": "行业动态"},
    }

    for cat_name, cat_items in categories.items():
        if not cat_items:
            continue
        cfg = section_config[cat_name]
        cards_html += f"""
        <div class="section section-{cfg['class']}">
            <div class="section-header">
                <span class="section-icon">{cfg['icon']}</span>
                <span class="section-title">{cat_name}</span>
                <span class="section-count">{len(cat_items)} 条</span>
            </div>
"""
        for item in cat_items:
            tags_html = "".join(
                f'<span class="tag tag-{tag.replace("#", "")}">{tag}</span>'
                for tag in item.get("tags", [])
            )
            external_mark = ""
            if item.get("is_external_source", False):
                external_mark = '<span class="source-external">📡 非固定源</span>'

            cards_html += f"""
            <div class="card">
                <div class="card-index">信息 {item['index']}</div>
                <div class="card-title">{item['title']}</div>
                <div class="card-date">📅 发布于 {item.get('date', '近期')}</div>
                <div class="card-field">
                    <span class="label">核心内容：</span>
                    <span class="value">{item['content']}</span>
                </div>
                <div class="card-field">
                    <span class="label">价值：</span>
                    <span class="value">{item['value']}</span>
                </div>
                <div class="tags">{tags_html}</div>
                <div class="card-link">
                    🔗 <a href="{item['link']}" target="_blank">{item['link']}</a>
                    {external_mark}
                </div>
            </div>
"""
        cards_html += "        </div>\n"

    # 替换模板占位符
    today_cn = datetime.now().strftime("%Y年%m月%d日")
    today = datetime.now().strftime("%Y-%m-%d")

    html = template_text
    html = html.replace("{{DATE}}", today)
    html = html.replace("{{DATE_CN}}", today_cn)
    html = html.replace("{{COUNT}}", str(total_count))
    html = html.replace("{{DATE_START}}", date_start)
    html = html.replace("{{DATE_END}}", date_end)
    html = html.replace("{{HIGHLIGHT_INDEX}}", str(highlight.get("index", 1)))
    html = html.replace("{{HIGHLIGHT_TITLE}}", highlight.get("title", ""))

    # 将卡片内容插入到模板中（在页脚之前）
    footer_marker = '<!-- 页脚 -->'
    html = html.replace(footer_marker, cards_html + "\n        " + footer_marker)

    return html


def save_html(html: str) -> Path:
    """保存 HTML 日报文件"""
    OUTPUT_DIR.mkdir(exist_ok=True)
    today = datetime.now().strftime("%Y-%m-%d")
    filename = f"{today}-AI日报.html"
    filepath = OUTPUT_DIR / filename
    filepath.write_text(html, encoding="utf-8")
    logger.info(f"日报已保存: {filepath}")
    return filepath


# ============================================================
# 消息推送
# ============================================================

def push_to_wecom_webhook(config: dict, brief_data: dict, html_path: Path, online_url: str = ""):
    """
    推送到企业微信群机器人（Webhook）

    设置方式：
    1. 在企微群聊中添加"群机器人"
    2. 获取 Webhook URL
    3. 填入 config.yaml 的 push.wecom_webhook.url
    """
    webhook_url = config.get("push", {}).get("wecom_webhook", {}).get("url", "")
    if not webhook_url:
        logger.warning("企微 Webhook URL 未配置，跳过推送")
        return

    items = brief_data.get("items", [])
    highlight = brief_data.get("highlight", {})
    today = datetime.now().strftime("%Y-%m-%d")

    # 构建 Markdown 消息
    lines = [
        f"# AI 日报 {today}",
        f"> 📰 收录 {len(items)} 条 | ⭐ 精选：{highlight.get('title', '')}",
        "",
    ]

    # 按分类展示
    categories = {"技术突破": "🔬", "产品应用": "🚀", "行业动态": "📊"}
    for cat_name, icon in categories.items():
        cat_items = [i for i in items if i.get("category") == cat_name]
        if cat_items:
            lines.append(f"### {icon} {cat_name}")
            for item in cat_items:
                tags_str = " ".join(item.get("tags", []))
                lines.append(f"- **{item['title']}** {tags_str}")
                lines.append(f"  {item['content'][:60]}...")
            lines.append("")

    lines.append(f"> 📎 完整日报已生成: {html_path.name}")
    if online_url:
        lines.append(f"> 🌐 [点击查看完整日报]({online_url})")

    markdown_content = "\n".join(lines)

    payload = {
        "msgtype": "markdown",
        "markdown": {"content": markdown_content},
    }

    try:
        resp = requests.post(webhook_url, json=payload, timeout=30)
        resp.raise_for_status()
        result = resp.json()
        if result.get("errcode") == 0:
            logger.info("企微群机器人推送成功")
        else:
            logger.error(f"企微推送失败: {result}")
    except Exception as e:
        logger.error(f"企微推送异常: {e}")


def push_to_wecom_app(config: dict, brief_data: dict, html_path: Path, online_url: str = ""):
    """
    推送到企业微信应用消息（发送到个人）

    设置方式：
    1. 在企业微信管理后台创建自建应用
    2. 获取 corpid、corpsecret、agentid
    3. 填入 config.yaml 的 push.wecom_app 配置
    """
    app_config = config.get("push", {}).get("wecom_app", {})
    corpid = app_config.get("corpid", "")
    corpsecret = app_config.get("corpsecret", "")
    agentid = app_config.get("agentid", 0)
    touser = app_config.get("touser", "@all")

    if not corpid or not corpsecret:
        logger.warning("企微应用配置不完整，跳过推送")
        return

    # 获取 access_token
    token_url = f"https://qyapi.weixin.qq.com/cgi-bin/gettoken?corpid={corpid}&corpsecret={corpsecret}"
    token_resp = requests.get(token_url, timeout=15)
    access_token = token_resp.json().get("access_token", "")

    if not access_token:
        logger.error("获取企微 access_token 失败")
        return

    items = brief_data.get("items", [])
    highlight = brief_data.get("highlight", {})
    today = datetime.now().strftime("%Y-%m-%d")

    # 构建文本卡片消息
    description = f"📰 收录 {len(items)} 条\n⭐ 精选：{highlight.get('title', '')}\n\n"
    for item in items[:5]:
        tags_str = " ".join(item.get("tags", []))
        description += f"• {item['title']} {tags_str}\n"
    if len(items) > 5:
        description += f"\n... 共 {len(items)} 条，点击查看完整日报"

    send_url = f"https://qyapi.weixin.qq.com/cgi-bin/message/send?access_token={access_token}"
    payload = {
        "touser": touser,
        "msgtype": "textcard",
        "agentid": agentid,
        "textcard": {
            "title": f"AI 日报 {today}",
            "description": description,
            "url": online_url or app_config.get("brief_url", ""),
            "btntxt": "查看完整日报",
        },
    }

    try:
        resp = requests.post(send_url, json=payload, timeout=30)
        result = resp.json()
        if result.get("errcode") == 0:
            logger.info("企微应用消息推送成功")
        else:
            logger.error(f"企微应用推送失败: {result}")
    except Exception as e:
        logger.error(f"企微应用推送异常: {e}")


def push_to_wechat_via_pushplus(config: dict, brief_data: dict, html_path: Path, online_url: str = ""):
    """
    通过 PushPlus 推送到个人微信

    设置方式：
    1. 访问 https://www.pushplus.plus/ 注册并关注公众号
    2. 获取 Token
    3. 填入 config.yaml 的 push.pushplus.token
    """
    token = config.get("push", {}).get("pushplus", {}).get("token", "")
    if not token:
        logger.warning("PushPlus Token 未配置，跳过推送")
        return

    items = brief_data.get("items", [])
    highlight = brief_data.get("highlight", {})
    today = datetime.now().strftime("%Y-%m-%d")

    # 构建 HTML 内容
    content = f"<h2>AI 日报 {today}</h2>"
    content += f"<p>📰 收录 {len(items)} 条 | ⭐ 精选：{highlight.get('title', '')}</p><hr>"

    categories = {"技术突破": "🔬", "产品应用": "🚀", "行业动态": "📊"}
    for cat_name, icon in categories.items():
        cat_items = [i for i in items if i.get("category") == cat_name]
        if cat_items:
            content += f"<h3>{icon} {cat_name}</h3><ul>"
            for item in cat_items:
                tags_str = " ".join(item.get("tags", []))
                content += f"<li><b>{item['title']}</b> {tags_str}<br>{item['content'][:80]}</li>"
            content += "</ul>"

    payload = {
        "token": token,
        "title": f"AI 日报 {today}",
        "content": content,
        "template": "html",
    }

    try:
        resp = requests.post("https://www.pushplus.plus/send", json=payload, timeout=30)
        result = resp.json()
        if result.get("code") == 200:
            logger.info("PushPlus 推送成功（个人微信）")
        else:
            logger.error(f"PushPlus 推送失败: {result}")
    except Exception as e:
        logger.error(f"PushPlus 推送异常: {e}")


def push_to_serverchan(config: dict, brief_data: dict, html_path: Path, online_url: str = ""):
    """
    通过 Server酱 推送到个人微信

    设置方式：
    1. 访问 https://sct.ftqq.com/ 注册并绑定微信
    2. 获取 SendKey
    3. 填入 config.yaml 的 push.serverchan.sendkey
    """
    sendkey = config.get("push", {}).get("serverchan", {}).get("sendkey", "")
    if not sendkey:
        logger.warning("Server酱 SendKey 未配置，跳过推送")
        return

    items = brief_data.get("items", [])
    highlight = brief_data.get("highlight", {})
    today = datetime.now().strftime("%Y-%m-%d")

    # 构建 Markdown 内容
    title = f"AI 日报 {today}"
    lines = [
        f"📰 收录 {len(items)} 条 | ⭐ 精选：{highlight.get('title', '')}",
        "",
    ]
    categories = {"技术突破": "🔬", "产品应用": "🚀", "行业动态": "📊"}
    for cat_name, icon in categories.items():
        cat_items = [i for i in items if i.get("category") == cat_name]
        if cat_items:
            lines.append(f"### {icon} {cat_name}")
            for item in cat_items:
                tags_str = " ".join(item.get("tags", []))
                lines.append(f"- **{item['title']}** {tags_str}")
            lines.append("")

    desp = "\n".join(lines)

    try:
        url = f"https://sctapi.ftqq.com/{sendkey}.send"
        resp = requests.post(url, data={"title": title, "desp": desp}, timeout=30)
        result = resp.json()
        if result.get("code") == 0:
            logger.info("Server酱推送成功（个人微信）")
        else:
            logger.error(f"Server酱推送失败: {result}")
    except Exception as e:
        logger.error(f"Server酱推送异常: {e}")


def deploy_to_github_pages(config: dict, html_path: Path) -> str:
    """
    将日报 HTML 自动提交并推送到 GitHub，通过 GitHub Pages 提供在线访问。

    返回在线访问 URL，失败则返回空字符串。
    """
    gh_config = config.get("push", {}).get("github_pages", {})
    if not gh_config.get("enabled", False):
        logger.info("GitHub Pages 未启用，跳过部署")
        return ""

    username = gh_config.get("username", "")
    repo = gh_config.get("repo", "")
    if not username or not repo:
        logger.warning("GitHub Pages 用户名或仓库名未配置")
        return ""

    try:
        # 确保在项目根目录执行 git 操作
        cwd = str(PROJECT_ROOT)

        # 检查是否已初始化 git
        git_dir = PROJECT_ROOT / ".git"
        if not git_dir.exists():
            logger.info("初始化 Git 仓库...")
            subprocess.run(["git", "init"], cwd=cwd, check=True, capture_output=True)
            subprocess.run(["git", "branch", "-M", "main"], cwd=cwd, check=True, capture_output=True)

        # 检查是否已设置 remote
        result = subprocess.run(["git", "remote", "-v"], cwd=cwd, capture_output=True, text=True)
        if f"github.com" not in result.stdout:
            remote_url = f"https://github.com/{username}/{repo}.git"
            subprocess.run(["git", "remote", "add", "origin", remote_url],
                         cwd=cwd, check=True, capture_output=True)
            logger.info(f"已添加 remote: {remote_url}")

        # 添加日报文件并提交
        # 只提交 AI简报 目录
        subprocess.run(["git", "add", "AI简报/"], cwd=cwd, check=True, capture_output=True)

        today = datetime.now().strftime("%Y-%m-%d")
        commit_msg = f"AI 日报 {today}"
        commit_result = subprocess.run(
            ["git", "commit", "-m", commit_msg],
            cwd=cwd, capture_output=True, text=True
        )
        if commit_result.returncode != 0:
            if "nothing to commit" in commit_result.stdout:
                logger.info("没有新的变更需要提交")
            else:
                logger.warning(f"Git commit 失败: {commit_result.stderr}")

        # 推送到 GitHub
        push_result = subprocess.run(
            ["git", "push", "-u", "origin", "main"],
            cwd=cwd, capture_output=True, text=True
        )
        if push_result.returncode != 0:
            logger.error(f"Git push 失败: {push_result.stderr}")
            return ""

        logger.info("已推送到 GitHub")

        # 构建 GitHub Pages URL
        # 文件名可能包含中文，需要 URL 编码
        relative_path = html_path.relative_to(PROJECT_ROOT)
        encoded_path = "/".join(quote(part) for part in relative_path.parts)
        online_url = f"https://{username}.github.io/{repo}/{encoded_path}"
        logger.info(f"在线访问链接: {online_url}")
        return online_url

    except Exception as e:
        logger.error(f"GitHub Pages 部署失败: {e}")
        return ""


def push_all(config: dict, brief_data: dict, html_path: Path):
    """执行所有已配置的推送渠道"""
    push_config = config.get("push", {})
    enabled_channels = push_config.get("enabled", [])

    if not enabled_channels:
        logger.info("未配置任何推送渠道，跳过推送")
        return

    # 先部署到 GitHub Pages，获取在线链接
    online_url = deploy_to_github_pages(config, html_path)

    push_handlers = {
        "wecom_webhook": push_to_wecom_webhook,
        "wecom_app": push_to_wecom_app,
        "pushplus": push_to_wechat_via_pushplus,
        "serverchan": push_to_serverchan,
    }

    for channel in enabled_channels:
        handler = push_handlers.get(channel)
        if handler:
            logger.info(f"推送到: {channel}")
            try:
                handler(config, brief_data, html_path, online_url)
            except Exception as e:
                logger.error(f"推送 {channel} 失败: {e}")
        else:
            logger.warning(f"未知推送渠道: {channel}")


# ============================================================
# 主流程
# ============================================================

def main():
    parser = argparse.ArgumentParser(description="AI 日报自动生成 & 推送")
    parser.add_argument("--no-push", action="store_true", help="仅生成不推送")
    parser.add_argument("--push-only", action="store_true", help="仅推送最新一期（不重新生成）")
    parser.add_argument("--days", type=int, default=3, help="搜索过去 N 天（默认 3）")
    args = parser.parse_args()

    config = load_config()

    today = datetime.now()
    date_end = today.strftime("%Y-%m-%d")
    date_start = (today - timedelta(days=args.days - 1)).strftime("%Y-%m-%d")

    if args.push_only:
        # 找到最新的日报文件
        html_files = sorted(OUTPUT_DIR.glob("*-AI日报.html"), reverse=True)
        if not html_files:
            logger.error("未找到任何日报文件")
            sys.exit(1)
        latest = html_files[0]
        logger.info(f"推送最新日报: {latest.name}")
        # 简化：仅发送通知，不解析内容
        brief_data = {"items": [], "highlight": {"title": latest.stem}}
        push_all(config, brief_data, latest)
        return

    # 第一步：调用 LLM 搜索并生成简报
    logger.info(f"开始生成 AI 日报 ({date_start} ~ {date_end})")
    brief_data = search_and_generate_brief(config, date_start, date_end)
    logger.info(f"获取到 {len(brief_data.get('items', []))} 条信息")

    # 第二步：生成 HTML
    html = generate_html(brief_data, date_start, date_end)
    html_path = save_html(html)

    # 第三步：推送
    if not args.no_push:
        push_all(config, brief_data, html_path)

    logger.info("完成！")


if __name__ == "__main__":
    main()
