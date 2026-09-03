"""手机查岗失败时的自动回退逻辑。

当手机截图超时或视觉分析失败时，若 auto_fallback 开启，则调用电脑屏幕
查岗工具（check_computer_screen）作为兜底。
"""
from __future__ import annotations

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent


def render_template(template: str, **kwargs) -> str:
    """将模板中的 {{key}} 占位符替换为对应值。"""
    result = template
    for key, value in kwargs.items():
        result = result.replace(f"{{{{{key}}}}}", str(value))
    return result


async def call_computer_screen_tool(
    event: AstrMessageEvent, tool_manager
) -> str | None:
    """调用电脑屏幕查岗工具（check_computer_screen）。

    设计文档原定通过 invoke_tool() 调用，但 AstrBot 4.x 无此 API，
    因此通过 get_llm_tool_manager().get_func(name) 获取工具对象，
    再调用其 handler（该 handler 在加载时已被绑定为 partial(handler, plugin)）。

    返回电脑查岗的结果字符串；工具不存在时返回 None。
    """
    tool = tool_manager.get_func("check_computer_screen")
    if tool is None or tool.handler is None:
        logger.warning("⚠️ 未找到电脑屏幕查岗工具 check_computer_screen，无法回退。")
        return None

    logger.info("🖥️ 正在调用电脑屏幕查岗工具进行回退...")
    try:
        result = await tool.handler(event)
        if result is None:
            return None
        # handler 返回 str 或 MessageEventResult
        if isinstance(result, str):
            return result
        # 兼容 MessageEventResult 类型
        return getattr(result, "message", None) or str(result)
    except Exception as e:
        logger.error(f"❌ 电脑屏幕查岗工具调用失败: {type(e).__name__} - {e}")
        return None


async def try_fallback(
    event: AstrMessageEvent,
    tool_manager,
    failed_device: str,
    error: str,
    fallback_template: str,
    both_failed_template: str,
) -> str:
    """尝试查看备用设备，失败则返回双失败提示词（设计文档 §6.6）。

    Args:
        failed_device: 失败的设备名称（"手机" 或 "电脑"）
        error: 失败原因，会填入模板的 {{error}} 占位符
        fallback_template: 回退成功时的提示模板
        both_failed_template: 两个设备都失败时的提示模板

    Returns:
        回退成功时为 "提示词\\n\\n备用设备的查看结果"；双失败时为双失败提示词。

    与设计文档的唯一差异：call_computer_screen_tool() 失败时返回 None 而不是抛异常
    （AstrBot 4.x 无 invoke_tool），因此双失败判定用 `is None` 而非 except。
    """
    backup_device = "电脑" if failed_device == "手机" else "手机"

    result = await call_computer_screen_tool(event, tool_manager)

    if result is None:
        combined_error = (
            f"{failed_device}：{error}；{backup_device}：查岗工具不可用或调用失败"
        )
        logger.warning(f"⚠️ 手机与电脑查岗均失败（{combined_error}）。")
        return render_template(
            both_failed_template,
            error=combined_error,
            failed_device=failed_device,
        )

    prompt = render_template(
        fallback_template,
        failed_device=failed_device,
        active_device=backup_device,
        error=error,
    )
    return f"{prompt}\n\n{result}"
