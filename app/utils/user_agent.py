def parse_os_from_user_agent(user_agent: str | None) -> str | None:
    """Best-effort operating system name extraction from a User-Agent header."""
    if not user_agent:
        return None

    ua = user_agent.lower()

    if "windows" in ua:
        return "Windows"
    if "android" in ua:
        return "Android"
    if "iphone" in ua or "ipad" in ua or "ipod" in ua:
        return "iOS"
    if "mac os x" in ua or "macintosh" in ua:
        return "macOS"
    if "linux" in ua:
        return "Linux"

    return "Unknown"
