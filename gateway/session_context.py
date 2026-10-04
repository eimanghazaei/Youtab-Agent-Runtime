"""Gateway compatibility exports for the shared session context.

The canonical ContextVars and functions live in agent.session_context so
metadata consumers do not import gateway services. These exports share the
same variables and tokens; session state has one owner.
"""

from agent.session_context import (
    _UNSET as _UNSET,
    session_context_engaged as session_context_engaged,
    _SESSION_PLATFORM as _SESSION_PLATFORM,
    _SESSION_SOURCE as _SESSION_SOURCE,
    _SESSION_CHAT_ID as _SESSION_CHAT_ID,
    _SESSION_CHAT_TYPE as _SESSION_CHAT_TYPE,
    _SESSION_CHAT_NAME as _SESSION_CHAT_NAME,
    _SESSION_THREAD_ID as _SESSION_THREAD_ID,
    _SESSION_USER_ID as _SESSION_USER_ID,
    _SESSION_USER_NAME as _SESSION_USER_NAME,
    _SESSION_KEY as _SESSION_KEY,
    _SESSION_ID as _SESSION_ID,
    _SESSION_UI_SESSION_ID as _SESSION_UI_SESSION_ID,
    _SESSION_MESSAGE_ID as _SESSION_MESSAGE_ID,
    _SESSION_PROFILE as _SESSION_PROFILE,
    _SESSION_ASYNC_DELIVERY as _SESSION_ASYNC_DELIVERY,
    _CRON_AUTO_DELIVER_PLATFORM as _CRON_AUTO_DELIVER_PLATFORM,
    _CRON_AUTO_DELIVER_CHAT_ID as _CRON_AUTO_DELIVER_CHAT_ID,
    _CRON_AUTO_DELIVER_THREAD_ID as _CRON_AUTO_DELIVER_THREAD_ID,
    _VAR_MAP as _VAR_MAP,
    set_current_session_id as set_current_session_id,
    set_session_vars as set_session_vars,
    clear_session_vars as clear_session_vars,
    reset_session_vars as reset_session_vars,
    get_session_env as get_session_env,
    NON_MESSAGING_SESSION_SURFACES as NON_MESSAGING_SESSION_SURFACES,
    session_is_messaging_surface as session_is_messaging_surface,
    declare_stateless_channel as declare_stateless_channel,
    async_delivery_supported as async_delivery_supported,
)
