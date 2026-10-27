"""Export the operation directory, or serve a provisioned world over HTTP/MCP."""
import argparse
import json
from pathlib import Path


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    export = sub.add_parser('export')
    export.add_argument('--catalog', required=True)
    export.add_argument('--out', required=True)
    for name in ['serve', 'mcp']:
        command = sub.add_parser(name)
        command.add_argument('--config', required=True)
        if name == 'serve':
            command.add_argument('--host', default='127.0.0.1')
            command.add_argument('--port', type=int, default=8080)
        else:
            command.add_argument('--actor', required=True)
    args = parser.parse_args(argv)
    if args.command == 'export':
        from mac_bench.construction.realize.catalog.entities import EntityCatalog
        from mac_bench.construction.realize.tools import build_registry
        registry = build_registry(EntityCatalog.load(args.catalog))
        out = Path(args.out)
        out.mkdir(parents=True, exist_ok=False)
        for name, tool in sorted(registry._tools.items()):
            (out / (name + '.json')).write_text(json.dumps(tool.archive(), ensure_ascii=False, indent=2) + '\n')
        (out / 'manifest.json').write_text(json.dumps({'revision': registry.revision,
            'catalog_revision': registry.catalog_revision, 'apps': registry.list_apps()['apps'],
            'tools': sorted(registry._tools), 'exclusions': registry.exclusions}, indent=2) + '\n')
        print(json.dumps(registry.list_apps()))
        return
    from mac_bench.construction.realize.tools.api import (
        create_http_app,
        create_mcp_server,
    )
    from mac_bench.construction.realize.tools.config import provision
    world, sessions = provision(json.loads(Path(args.config).read_text()))
    try:
        if args.command == 'serve':
            import uvicorn
            uvicorn.run(create_http_app(sessions), host=args.host, port=args.port)
        else:
            agents = {s.actor: s for s in sessions.values()}
            if args.actor not in agents:
                raise ValueError('Actor is not provisioned')
            create_mcp_server(agents[args.actor]).run(transport='stdio')
    finally:
        world.close()
        world.journal.close()


if __name__ == '__main__':
    main()
