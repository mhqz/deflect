# Copyright (c) 2021, eQualit.ie inc.
# All rights reserved.
#
# This source code is licensed under the BSD-style license found in the
# LICENSE file in the root directory of this source tree.

import nginx
import os
import shutil
import tarfile
import json
from jinja2 import Template
from pyaml_env import parse_config

import logging
from util.helpers import (
        get_logger,
        get_config_yml_path,
        path_to_input,
        path_to_output,
)

logger = get_logger(__name__, logging_level=logging.DEBUG)


def redirect_to_https_server(site: dict):
    """
    Add a 301 to https
    """
    return nginx.Conf(
        nginx.Server(
            nginx.Key('set', "$loc_in \"redir_to_ssl\""),
            nginx.Key('set', "$loc_out \"redir_to_ssl\""),
            nginx.Key('server_name', " ".join(site["server_names"])),
            nginx.Key('listen', '80'),
            nginx.Key(
                # note that we always redirect to the non-www hostname
                'return', "301 https://$server_name$request_uri/")
        )
    )


def ssl_certificate_and_key(dconf, site):
    keys = []

    if site.get("uploaded_cert_bundle_name"):
        chain_path = f"/etc/ssl-uploaded/{site['public_domain']}.cert-and-chain"
        key_path = f"/etc/ssl-uploaded/{site['public_domain']}.key"
    else:
        chain_path = f"/etc/ssl/sites/{site['public_domain']}/fullchain1.pem"
        key_path = f"/etc/ssl/sites/{site['public_domain']}/privkey1.pem"

    keys.append(nginx.Key('ssl_certificate', chain_path))
    keys.append(nginx.Key('ssl_certificate_key', key_path))
    return keys


def proxy_to_upstream_server(site, dconf, edge_https, origin_https):
    server = nginx.Server()

    server.add(nginx.Key('server_name', " ".join(site["server_names"])))
    server.add(nginx.Key('proxy_set_header', 'Host $host'))

    if edge_https:
        server.add(nginx.Key('listen', '443 ssl http2'))
        server.add(*ssl_certificate_and_key(dconf, site))
        server.add(nginx.Key('ssl_ciphers', dconf["ssl_ciphers"]))
    else:
        server.add(nginx.Key('listen', '80'))

    for pattern in sorted(site['password_protected_paths']):
        server.add(
            pass_prot_location(pattern, origin_https, site)
        )

    # XXX i think the order we match these in matters
    for exc in sorted(site['cache_exceptions']):
        server.add(
            cache_exc_location(exc, origin_https, site)
        )

    server.add(
        static_files_location(site, dconf, edge_https, origin_https)
    )

    server.add(
        slash_location(origin_https, site)
    )

    server.add(
        access_denied_location(site)
    )

    server.add(
        access_granted_location_block(site, dconf, edge_https, origin_https)
    )

    server.add(
        fail_open_location_block(site, dconf, edge_https, origin_https)
    )

    server.add(
        fail_closed_location_block(site, dconf, edge_https, origin_https)
    )

    return server


def proxy_pass_to_banjax_keys(origin_https, site):
    return [
        # nginx.Key('access_log', "off"),  # XXX maybe log to a different file

        # nginx.Key('proxy_cache', "auth_requests_cache"),
        nginx.Key('proxy_cache_key',
                  '"$remote_addr $host $cookie_deflect_challenge"'),

        nginx.Key('proxy_set_header', "X-Requested-Host $host"),
        nginx.Key('proxy_set_header', "X-Client-IP $remote_addr"),
        nginx.Key('proxy_set_header', "X-Requested-Path $request_uri"),
        nginx.Key('proxy_pass_request_body', "off"),
        # XXX i just want to discard the path
        # TODO: use config for port, ip
        nginx.Key('proxy_pass', "http://127.0.0.1:8081/auth_request?")
    ]


def pass_prot_location(pattern, origin_https, site):
    # XXX triage and review all my location matching patterns at some point.
    # XXX i don't think we're ensuring there aren't overlapping patterns?
    if "." in pattern:
        location = nginx.Location(f"= /{pattern}")
    else:
        location = nginx.Location(f"/{pattern}/")

    location.add(nginx.Key('set', "$loc_in \"pass_prot\""))

    location.add(nginx.Key('proxy_cache_valid', '0'))

    location.add(nginx.Key('error_page', "500 501 502 @fail_closed"))
    location.add(*proxy_pass_to_banjax_keys(origin_https, site))

    return location


# XXX looks like i'm not converting these in the site dict code right now
def cache_exc_location(exc, origin_https, site):
    location = nginx.Location(f"~* {exc['location_regex']}")

    location.add(nginx.Key('set', "$loc_in \"cache_exc\""))

    location.add(
        *default_site_content_cache_include_conf(
            exc['cache_time_minutes'], site
        ))

    location.add(nginx.Key('error_page', "500 @access_granted"))
    location.add(*proxy_pass_to_banjax_keys(origin_https, site))

    return location


def slash_location(origin_https, site):
    location = nginx.Location('/')
    location.add(nginx.Key('set', "$loc_in \"slash_block\""))
    # location.add(*default_site_content_cache_include_conf(site['default_cache_time_minutes'], site))

    location.add(nginx.Key('error_page', "500 501 502 @fail_open"))
    location.add(*proxy_pass_to_banjax_keys(origin_https, site))

    return location

# XXX somehow this needs to be an @access_granted_cache_static block or something


def static_files_location(site, global_config, edge_https, origin_https):
    # XXX how to avoid sending js challenger pages to (embedded) filetypes?
    location = nginx.Location(
        '~* \.(css|js|json|png|gif|ico|jpg|jpeg|svg|ttf|woff|woff2)$')
    location.add(nginx.Key('set', "$loc_in \"static_file\""))
    location.add(nginx.Key('set', "$loc_out \"static_file\""))
    location_contents = _access_granted_fail_open_location_contents(
        site, global_config, edge_https, origin_https)
    location.add(*location_contents)

    # location.add(nginx.Key('proxy_cache_valid', '200 302 10m'))  # XXX config
    # location.add(nginx.Key('proxy_cache_valid', '404 30s'))  # XXX other error pages?

    # location.add(nginx.Key('error_page', "500 @access_granted"))
    # location.add(*proxy_pass_to_banjax_keys(origin_https, site))

    return location


def _access_granted_fail_open_location_contents(
        site, global_config, edge_https, origin_https
):
    location_contents = []
    location_contents += default_site_content_cache_include_conf(
        site['default_cache_time_minutes'], site
    )

    limit_except = nginx.LimitExcept(
        'GET POST PUT MKCOL COPY MOVE OPTIONS PROPFIND PROPPATCH LOCK UNLOCK PATCH')
    limit_except.add(nginx.Key('deny', 'all'))
    location_contents.append(limit_except)
    location_contents.append(nginx.Key('add_header', "X-Deflect-Cache $upstream_cache_status"))
    # location_contents.append(nginx.Key('add_header', "X-Deflect-upstream_addr $upstream_addr"))
    location_contents.append(nginx.Key('add_header', "X-Deflect-upstream_response_time $upstream_response_time"))
    location_contents.append(nginx.Key('proxy_set_header', "X-Forwarded-For $proxy_add_x_forwarded_for"))
    location_contents.append(nginx.Key('proxy_set_header', "Host $host"))
    location_contents.append(nginx.Key('proxy_hide_header', "Upgrade"))
    location_contents.append(nginx.Key('proxy_ssl_name', '$host'))
    location_contents.append(nginx.Key('proxy_pass_request_body', "on"))

    if origin_https:
        location_contents.append(nginx.Key(
            'proxy_pass', f"https://{site['origin_ip']}:{site['origin_https_port']}"))
    else:
        location_contents.append(nginx.Key(
            'proxy_pass', f"http://{site['origin_ip']}:{site['origin_http_port']}"))

    return location_contents


def access_granted_location_block(site, global_config, edge_https, origin_https):
    location = nginx.Location("@access_granted")
    location.add(nginx.Key('set', "$loc_out \"access_granted\""))
    location_contents = _access_granted_fail_open_location_contents(
        site, global_config, edge_https, origin_https)
    location.add(*location_contents)
    return location


def fail_open_location_block(site, global_config, edge_https, origin_https):
    location = nginx.Location("@fail_open")
    location.add(nginx.Key('set', "$loc_out \"fail_open\""))
    location_contents = _access_granted_fail_open_location_contents(
        site, global_config, edge_https, origin_https)
    location.add(*location_contents)
    return location


def port_80_server_block(dconf, site, http_req_does):
    if http_req_does == 'redirect':
        return redirect_to_https_server(site)

    elif http_req_does == 'http_proxy_pass':
        # legacy behavior. maybe we want to upgrade http -> https when we can?
        return proxy_to_upstream_server(
            site, dconf, edge_https=False, origin_https=False)

    else:
        raise Exception(f"unrecognized http_request_does: {http_req_does}")


def port_443_server_block(dconf, site, https_req_does):
    if https_req_does == 'https_proxy_pass':
        return proxy_to_upstream_server(
            site, dconf, edge_https=True, origin_https=True)

    elif https_req_does == 'http_proxy_pass':
        return proxy_to_upstream_server(
            site, dconf, edge_https=True, origin_https=False)

    else:
        raise Exception(f"unrecognized https_request_does: {https_req_does}")


def per_site_include_conf(site, dconf):
    nconf = nginx.Conf()

    # 301 to https:// or proxy_pass to origin port 80
    http_req_does = site['http_request_does']
    if http_req_does != "nothing":
        nconf.add(port_80_server_block(dconf, site, http_req_does))

    # proxy_pass to origin port 80 or 443
    https_req_does = site['https_request_does']
    if https_req_does != "nothing":
        nconf.add(port_443_server_block(dconf, site, https_req_does))

    return nconf


# https://serverfault.com/questions/578648/properly-setting-up-a-default-nginx-server-for-https/1044022#1044022
# this keeps nginx from choosing some random site if it can't find one...
def empty_catchall_server():
    return nginx.Server(
        nginx.Key('listen', "80 default_server"),
        nginx.Key('listen', "443 ssl http2 default_server"),
        nginx.Key('listen', "[::]:80 default_server"),
        nginx.Key('listen', "[::]:443 ssl http2 default_server"),

        nginx.Key('server_name', "_"),

        nginx.Key('ssl_ciphers', "aNULL"),
        nginx.Key('ssl_certificate', "data:$empty"),
        nginx.Key('ssl_certificate_key', "data:$empty"),

        nginx.Key('return', '444')
    )


# the built-in stub_status route shows us the number of active connections.
# /info is useful for checking what version of config is loaded.
def info_and_stub_status_server(timestamp):
    return nginx.Server(
        nginx.Key('listen', "80"),
        nginx.Key('server_name', "127.0.0.1"),  # metricbeat doesn't allow setting the Host header
        nginx.Key('allow', "127.0.0.1"),
        nginx.Key('deny', "all"),
        nginx.Key('access_log', "off"),

        nginx.Location('/info',
            nginx.Key('return', f"200 \"{timestamp}\\n\"")),

        nginx.Location('/stub_status',
            nginx.Key('stub_status', None))
    )


def banjax_server():
    return nginx.Server(
        nginx.Key('listen', "80"),
        nginx.Key('server_name', "banjax"),
        nginx.Key('allow', "127.0.0.1"),
        nginx.Key('deny', "all"),
        nginx.Key('access_log', "off"),  # XXX?

        # should we just pass every request?
        nginx.Location('/info',
            nginx.Key('proxy_pass', "http://127.0.0.1:8081/info")),

        nginx.Location('/decision_lists',
            nginx.Key('proxy_pass', "http://127.0.0.1:8081/decision_lists")),

        nginx.Location('/rate_limit_states',
            nginx.Key('proxy_pass', "http://127.0.0.1:8081/rate_limit_states")),
    )


def cache_purge_server():
    return nginx.Server(
        nginx.Key('listen', "80"),
        nginx.Key('server_name', '"cache_purge"'),
        nginx.Key('access_log', "off"),
        nginx.Key('allow', "127.0.0.1"),
        nginx.Key('deny', "all"),

        nginx.Location('~ /auth_requests/(.*)',
            nginx.Key('proxy_cache_purge', "auth_requests_cache $1")),

        nginx.Location('~ /site_content/(.*)',
            nginx.Key('proxy_cache_purge', "site_content_cache $1")),
    )


def http_block(dconf, timestamp):
    http = nginx.Http()
    http.add(nginx.Key('server_names_hash_bucket_size', '128'))
    http.add(nginx.Key('log_format', "main '$time_local | $status | $request_time (s)| $remote_addr | $request'"))
    http.add(nginx.Key('log_format', "banjax_format '$msec $remote_addr $request_method $host $request $http_user_agent'"))
    http.add(nginx.Key('log_format',
        "logstash_format '$remote_addr $remote_user [$time_local] \"$request\" $scheme $host "
        "$status $bytes_sent \"$http_user_agent\" $upstream_cache_status \"$sent_http_content_type\" "
        "$proxy_host $request_time $scheme://$proxy_host:$proxy_port$uri \"$http_referer\" \"$http_x_forwarded_for\"'"))

    # renaming so they don't collide in ES/kibana
    http.add(nginx.Key('log_format', """ json_combined escape=json
        '{'
            '"time_local":"$time_local",'
            '"remote_addr":"$remote_addr",'
            '"request_host":"$host",'
            '"request_uri":"$request_uri",'
            '"ngx_status": "$status",'
            '"ngx_body_bytes_sent": "$body_bytes_sent",'
            '"ngx_upstream_addr": "$upstream_addr",'
            '"ngx_upstream_cache_status": "$upstream_cache_status",'
            '"ngx_upstream_response_time": "$upstream_response_time",'
            '"ngx_request_time": "$request_time",'
            '"http_referrer": "$http_referer",'
            '"http_user_agent": "$http_user_agent",'
            '"ngx_loc_in": "$loc_in",'
            '"ngx_loc_out": "$loc_out",'
            '"ngx_loc_in_out": "${loc_in}-${loc_out}"'
        '}' """
    ))

    http.add(nginx.Key('error_log', "/dev/stdout warn"))
    http.add(nginx.Key('access_log', "/var/log/nginx/access.log json_combined"))
    http.add(nginx.Key('access_log', "/var/log/nginx/banjax-format.log banjax_format"))
    http.add(nginx.Key('access_log', "/var/log/nginx/nginx-logstash-format.log logstash_format"))

    http.add(nginx.Key('proxy_cache_path', "/data/nginx/auth_requests_cache keys_zone=auth_requests_cache:10m"))
    http.add(nginx.Key('proxy_cache_path', "/data/nginx/site_content_cache keys_zone=site_content_cache:10m max_size=50g"))
    http.add(nginx.Key('client_max_body_size', "2G"))  # XXX think about this

    http.add(nginx.Key('proxy_set_header', "X-Forwarded-For $proxy_add_x_forwarded_for"))

    # https://serverfault.com/questions/578648/properly-setting-up-a-default-nginx-server-for-https/1044022#1044022
    # this keeps nginx from choosing some random site if it can't find one
    http.add(nginx.Map('"" $empty', nginx.Key("default", '""')))
    http.add(empty_catchall_server())

    # /info and /stub_status
    http.add(info_and_stub_status_server(timestamp))

    # exposing a few banjax endpoints
    http.add(banjax_server())

    # purge the auth_requests or site_content caches
    http.add(cache_purge_server())

    # include all the per-site files
    http.add(nginx.Key('include', "/etc/nginx/sites.d/*.conf"))

    return http


def top_level_conf(dconf, timestamp):
    nconf = nginx.Conf()

    nconf.add(nginx.Key('load_module', '/usr/lib/nginx/modules/ngx_http_cache_purge_module_torden.so'))

    nconf.add(nginx.Events(nginx.Key('worker_connections', '1024')))

    nconf.add(http_block(dconf, timestamp))

    return nconf


def default_site_content_cache_include_conf(cache_time_minutes, site):
    return [
        nginx.Key('proxy_cache', "site_content_cache"),
        nginx.Key('proxy_cache_key', '"$host $scheme $uri $is_args $args"'),
        nginx.Key('proxy_cache_valid', f"any {str(cache_time_minutes)}")
    ]


def access_denied_location(site):
    location = nginx.Location('@access_denied')
    location.add(nginx.Key('set', "$loc_out \"access_denied\""))
    location.add(nginx.Key('return', "403 \"access denied\""))
    return location


def fail_closed_location_block(site, global_config, edge_https, origin_https):
    location = nginx.Location('@fail_closed')
    location.add(nginx.Key('set', "$loc_out \"fail_closed\""))
    location.add(nginx.Key('return', "500 \"error talking to banjax, failing closed\""))
    return location


def get_output_dir(formatted_time, dnet):
    return os.path.join(path_to_output(), formatted_time, f"etc-nginx-{dnet}")


# XXX ugh this needs redoing
def generate_nginx_config(all_sites, config, formatted_time):
    # clear out directories
    for dnet in sorted(config['dnets']):
        output_dir = get_output_dir(formatted_time, dnet)
        if os.path.isdir(output_dir):
            logger.debug(f"removing {output_dir}")
            shutil.rmtree(output_dir)
        os.makedirs(output_dir)

        with open(output_dir + "/nginx.conf", "w") as f:
            nginx.dump(top_level_conf(config, formatted_time), f)

        info_dir = output_dir + "/info"
        os.mkdir(info_dir)
        with open(info_dir + "/info", "w") as f:
            f.write(json.dumps({"config_version": formatted_time}))

        os.mkdir(output_dir + "/sites.d")

    # write out the client sites
    for name, site in all_sites['client'].items():
        public_domain = site['public_domain']
        output_dir = get_output_dir(formatted_time, site['dnet'])
        # XXX check for cert existence properly
        if False:
            logger.debug(f"!!! http-only for {site} because we couldn't find certs !!!")
            conf = nginx.Conf()
            conf.add(proxy_to_upstream_server(site, config, edge_https=False, origin_https=False))
            with open(f"{output_dir}/sites.d/{public_domain}.conf", "w") as f:
                nginx.dump(conf, f)
        with open(f"{output_dir}/sites.d/{public_domain}.conf", "w") as f:
            nginx.dump(per_site_include_conf(site, config), f)

    # write out the system sites
    for name, site in all_sites['system'].items():
        # make these live on every dnet?
        for dnet in config['dnets']:
            output_dir = get_output_dir(formatted_time, dnet)
            with open(f"{path_to_input()}/templates/system_site_nginx.conf.j2", "r") as tf:
                template = Template(tf.read())
                with open(f"{output_dir}/sites.d/{name}.conf", "w") as f:
                    f.write(template.render(
                        server_name=name,
                        cert_name=name,
                        ssl_ciphers=config['ssl_ciphers'],
                        proxy_pass=f"http://{site['origin_ip']}:{site['origin_http_port']}",
                    ))

    # create tarfiles
    for dnet in config['dnets']:
        output_dir = get_output_dir(formatted_time, dnet)
        if os.path.isfile(f"{output_dir}.tar"):
            os.remove(f"{output_dir}.tar")

        with tarfile.open(f"{output_dir}.tar", "x") as tar:
            tar.add(output_dir, arcname=".")


if __name__ == "__main__":
    from orchestration.shared import get_all_sites

    config = parse_config(get_config_yml_path())

    all_sites, formatted_time = get_all_sites()

    generate_nginx_config(all_sites, config, formatted_time)
