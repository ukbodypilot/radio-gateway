"""POST route handlers for the web server.

Handlers are organised by domain — see the web_routes_<domain>.py modules.
This module re-exports them so the web_server dispatcher keeps the
historical `import web_routes_post as _rp; _rp.handle_foo(...)` call pattern.

Add a new handler by writing it in the appropriate domain module and
adding its name here.
"""

from web_routes_transcribe  import (
    handle_transcription_query, handle_transcribe_config,
    handle_transcribe_worker_register,
)
from web_routes_radio  import (
    handle_d75cmd, handle_ic7100cmd, handle_kv4pcmd, handle_linkcmd, handle_catcmd,
    handle_sdrcmd, handle_packet_cmd,
)
from web_routes_audio  import (
    handle_testloop, handle_bgm, handle_announcer, handle_mixer, handle_proc_toggle,
    handle_tracecmd, handle_routing_cmd, handle_loop_export,
    handle_recordingsdelete,
)
from web_routes_text  import handle_aitext, handle_cw, handle_tts
from web_routes_fart  import handle_fart_preview, handle_fart_send
from web_routes_automation  import (handle_automationcmd, handle_refreshsounds,
                                    handle_soundboard_categories, handle_tts_engine)
from web_routes_system  import (
    handle_key, handle_gpscmd, handle_reboothost,
    handle_restartgateway,
    handle_exit, handle_config_form, handle_gdrive_publish_tunnel,
)
from web_routes_manager  import (
    handle_manager_toggle, handle_manager_config,
    handle_manager_save, handle_manager_run, handle_manager_ack,
)
