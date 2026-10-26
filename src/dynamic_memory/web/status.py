"""Static UI copy for IC-012 error codes; no build state inference or DTOs."""

# Only allowlisted codes may reach the page. Never display exception messages.
ERROR_MESSAGES = {
    "DATA_NOT_LOADED": "数据未加载：请由服务端完成数据加载后刷新。",
    "DATA_LOAD_FAILED": "数据加载失败：请检查服务端数据加载结果。",
    "BUILD_NOT_FOUND": "记忆未构建：原始数据与记忆构建是不同步骤，请等待离线构建完成。",
    "ARTIFACT_INCOMPLETE": "记忆部分构建：当前产物不完整，不能作为完整记忆库浏览。",
    "MEMORY_BUILD_FAILED": "记忆构建失败：请检查服务端构建结果。",
    "ARTIFACT_CORRUPT": "记忆产物损坏：请由服务端核验或重新构建。",
    "ARTIFACT_STALE": "记忆产物已过期：请由服务端更新后刷新。",
    "INDEX_STALE": "检索索引已过期：请由服务端更新索引。",
    "SAMPLE_NOT_FOUND": "样本不存在：请刷新服务并重新选择样本。",
    "MEMORY_NOT_FOUND": "记忆不存在：记录可能已更新，请重新选择。",
    "SOURCE_NOT_FOUND": "原始轮次不存在：请检查该来源引用。",
    "SAMPLE_MISMATCH": "来源或记录不属于当前样本，已拒绝展示。",
}


def known_error_code(error: Exception) -> str | None:
    code = getattr(error, "code", None)
    return code if isinstance(code, str) and code in ERROR_MESSAGES else None
