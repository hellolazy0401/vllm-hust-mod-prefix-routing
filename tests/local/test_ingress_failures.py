import importlib.util
from pathlib import Path
import aiohttp
from aiohttp import web
from aiohttp.test_utils import TestServer, TestClient
import pytest

ROOT=Path(__file__).resolve().parents[2]
spec=importlib.util.spec_from_file_location('ingress',ROOT/'benchmarks/prefix_routing_random_proxy.py')
mod=importlib.util.module_from_spec(spec); spec.loader.exec_module(mod)

@pytest.mark.asyncio
async def test_disconnect_before_headers_is_502_without_post_retry(tmp_path):
    calls=[]
    async def upstream(request):
        calls.append(await request.read())
        request.transport.close()
        return web.Response()
    app=web.Application(); app.router.add_post('/',upstream)
    async with TestServer(app) as backend:
        proxy=mod.RandomProxy([str(backend.make_url('/'))],0,tmp_path/'stats.json','close')
        frontend=web.Application(); frontend.on_startup.append(proxy.start); frontend.on_cleanup.append(proxy.stop)
        frontend.router.add_post('/',proxy.forward)
        async with TestClient(TestServer(frontend)) as client:
            response=await client.post('/',data=b'one inference')
            assert response.status==502
            assert 'ServerDisconnectedError' in (await response.json())['error']
            assert len(calls)==1
            assert proxy.upstream_errors==1
            assert proxy.session.connector.force_close

@pytest.mark.asyncio
async def test_disconnect_mid_stream_is_not_successful_eof(tmp_path):
    calls=[]
    async def upstream(request):
        calls.append(1)
        response=web.StreamResponse(); await response.prepare(request)
        await response.write(b'data: partial\n\n')
        request.transport.close()
        return response
    app=web.Application(); app.router.add_post('/',upstream)
    async with TestServer(app) as backend:
        proxy=mod.RandomProxy([str(backend.make_url('/'))],0,tmp_path/'stats.json')
        frontend=web.Application(); frontend.on_startup.append(proxy.start); frontend.on_cleanup.append(proxy.stop)
        frontend.router.add_post('/',proxy.forward)
        async with TestClient(TestServer(frontend)) as client:
            with pytest.raises(aiohttp.ClientError):
                response=await client.post('/',data=b'one inference')
                await response.read()
            assert len(calls)==1
            assert proxy.upstream_errors==1
