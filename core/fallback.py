from astrbot.api import logger


async def try_fallback(
    failed_device: str,
    error: str,
    event,
    config: dict,
) -> str:
    """
    手机查看失败时，尝试调用电脑屏幕查岗工具作为备份。
    如果备份也失败，返回双失败提示词。

    AstrBot 没有跨插件 invoke_tool API，因此直接导入
    screen_spy 插件的截图+视觉逻辑复用。
    """
    fallback_prompt = config.get(
        "fallback_prompt_template",
        "当前{{failed_device}}因为{{error}}无法查看，系统已返回目前活跃的{{active_device}}设备，请根据人设与上下文向用户发送信息。",
    )
    both_failed_prompt = config.get(
        "both_failed_prompt_template",
        "无法查看当前用户电脑与手机的屏幕，原因可能是{{error}}，可能用户正在忙或者在睡觉，请根据人设向用户发送信息。",
    )

    backup_device = "电脑" if failed_device == "手机" else "手机"

    try:
        # 尝试直接调用 screen_spy 插件里的截图+视觉逻辑
        result = await _call_screen_spy(config)

        prompt = (
            fallback_prompt
            .replace("{{failed_device}}", failed_device)
            .replace("{{active_device}}", backup_device)
            .replace("{{error}}", error)
        )
        return f"{prompt}\n\n{result}"

    except Exception as backup_error:
        logger.warning(f"[phone_spy] 故障转移也失败: {backup_error}")
        combined_error = f"{failed_device}：{error}；{backup_device}：{backup_error}"
        return both_failed_prompt.replace("{{error}}", combined_error)


async def _call_screen_spy(config: dict) -> str:
    """
    直接复用 screen_spy 插件的截图+视觉逻辑。
    如果 screen_spy 不存在则抛出 ImportError。
    """
    import importlib
    import aiohttp
    import base64

    # 尝试导入 screen_spy 配置，如果插件不存在则退出
    try:
        # screen_spy 插件目录名
        screen_spy_mod = importlib.import_module(
            "data.plugins.plugin_upload_astrbot_plugin_screen_monitor.main"
        )
    except ModuleNotFoundError:
        raise ImportError("screen_spy 插件未安装，无法故障转移到电脑截图")

    # 从 screen_spy 的同名配置读取连接参数（用户需在 screen_spy 里配好）
    ss_config = getattr(screen_spy_mod, "_phone_spy_fallback_config", None)
    if ss_config is None:
        # 降级：使用 phone_spy 配置里的视觉参数 + screen_spy 默认截图地址
        host = "127.0.0.1"
        port = 6878
        api_url = config.get("vision_api_url", "")
        api_key = config.get("vision_api_key", "")
        model = config.get("vision_model", "gpt-4o")
        prompt = "你是一个查岗助手，请直接回复，详细描述这张电脑屏幕截图的内容。"
        timeout_ss = 30
        timeout_vision = config.get("vision_api_timeout", 60)
    else:
        host = ss_config.get("server_host", "127.0.0.1")
        port = ss_config.get("server_port", 6878)
        api_url = ss_config.get("vision_api_url", config.get("vision_api_url", ""))
        api_key = ss_config.get("vision_api_key", config.get("vision_api_key", ""))
        model = ss_config.get("vision_model_name", config.get("vision_model", "gpt-4o"))
        prompt = ss_config.get("vision_prompt", "请描述电脑屏幕内容。")
        timeout_ss = ss_config.get("screenshot_timeout", 30)
        timeout_vision = ss_config.get("vision_api_timeout", 60)

    screenshot_url = f"http://{host}:{port}/screenshot"
    async with aiohttp.ClientSession() as session:
        async with session.get(screenshot_url, timeout=timeout_ss) as resp:
            if resp.status != 200:
                raise RuntimeError(f"电脑截图服务返回 {resp.status}")
            img_data = await resp.read()

    from .vision import analyze_screenshot
    description = await analyze_screenshot(
        img_data, api_url, api_key, model, prompt, timeout=timeout_vision
    )
    return f"查岗成功，用户当前的电脑屏幕内容描述如下：\n{description}"
