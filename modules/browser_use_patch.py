"""
Compatibility patch for browser-use 0.4.5 + patchright.

browser-use's BrowserLaunchPersistentContextArgs (a pydantic model) always
declares a 'devtools' field, so even excluding it when constructing that
model just lets pydantic refill it from the default - it reappears when
browser_use later calls .model_dump(mode='json') and passes the result as
**kwargs to BrowserType.launch_persistent_context(). Standard Playwright
accepts 'devtools' there, but patchright's launch_persistent_context() does
not define that parameter at all, so every launch under STEALTH_MODE=True
(which routes through patchright) raises:
    TypeError: BrowserType.launch_persistent_context() got an unexpected
    keyword argument 'devtools'

Patch patchright's BrowserType.launch_persistent_context itself to drop
'devtools' from incoming kwargs before delegating to the original method.
Import this before any Browser/BrowserConfig is constructed.
"""
from patchright.async_api import BrowserType

_original_launch_persistent_context = BrowserType.launch_persistent_context


async def _launch_persistent_context(self, *args, **kwargs):
    kwargs.pop("devtools", None)
    return await _original_launch_persistent_context(self, *args, **kwargs)


BrowserType.launch_persistent_context = _launch_persistent_context
