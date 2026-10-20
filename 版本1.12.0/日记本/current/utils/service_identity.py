"""读取日记项目自己的稳定身份；不自动生成，不依赖主站或业务数据。"""

import ipaddress
import json
import re
from uuid import UUID

from werkzeug.middleware.proxy_fix import ProxyFix
from utils.paths import IDENTITY_ROOT

def load_identity(path):
    try:
        record = json.loads(path.read_text(encoding='utf-8-sig'))
        if not isinstance(record, dict):
            raise ValueError('身份文件必须是 JSON 对象')
        service_id = record.get('service_id')
        if not isinstance(service_id, str) or not service_id.startswith('urn:uuid:'):
            raise ValueError('service_id 必须是 urn:uuid 格式')
        if UUID(service_id).urn != service_id:
            raise ValueError('service_id 必须使用规范的 UUID 格式')
        protocol = record.get('identity_protocol')
        if type(protocol) is not int or protocol != 1:
            raise ValueError('当前版本只支持身份协议 1')
        return service_id, protocol
    except (OSError, ValueError) as exc:
        raise RuntimeError(f'无法加载日记身份文件 {path}：请保留项目 identity 目录及兼容的 service.json') from exc


def load_service_group(path):
    load_identity(path)
    try:
        record = json.loads(path.read_text(encoding='utf-8-sig'))
        if record.get('service_role') != 'service-group':
            raise ValueError('深圳湾日记入口必须声明 service-group 角色')
        for key in ('service_name', 'gateway_name'):
            if not isinstance(record.get(key), str) or not record[key].strip():
                raise ValueError(f'{key} 必须是非空名称')
        gateway_id = record.get('gateway_service_id')
        if (not isinstance(gateway_id, str) or not gateway_id.startswith('urn:uuid:')
                or UUID(gateway_id).urn != gateway_id or gateway_id == record['service_id']):
            raise ValueError('管理入口必须具有独立的规范组件 ID')
        return record
    except (OSError, ValueError) as exc:
        raise RuntimeError('无法加载深圳湾日记组身份，请保留组角色与管理入口身份') from exc


SERVICE_INFO = load_service_group(IDENTITY_ROOT / 'service.json')
SERVICE_ID, IDENTITY_PROTOCOL = SERVICE_INFO['service_id'], SERVICE_INFO['identity_protocol']
APP_VERSION = 't11'


class LocalProxyPrefix:
    """只接受本机代理传来的路径前缀，独立访问仍使用根路径。"""

    def __init__(self, application):
        self.application = application
        self.with_prefix = ProxyFix(application, x_for=0, x_proto=0, x_host=0,
                                    x_port=0, x_prefix=1)

    def __call__(self, environ, start_response):
        try:
            local = ipaddress.ip_address(environ.get('REMOTE_ADDR', '')).is_loopback
        except ValueError:
            local = False
        prefix = environ.get('HTTP_X_FORWARDED_PREFIX', '')
        if local and re.fullmatch(r'(?:/[A-Za-z0-9_-]+)+', prefix):
            return self.with_prefix(environ, start_response)
        return self.application(environ, start_response)
