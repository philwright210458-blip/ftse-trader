import http from 'node:http';
import {readFile,writeFile,mkdir,rename,readdir} from 'node:fs/promises';
import {fileURLToPath} from 'node:url';
import path from 'node:path';
import {handleAPI,MAX_BODY} from './api.mjs';
const here=path.dirname(fileURLToPath(import.meta.url));
export class FileStore {
  constructor(directory){this.directory=directory;this.queue=Promise.resolve();}
  async latest(){try{return JSON.parse(await readFile(path.join(this.directory,'current.json'),'utf8'));}catch(e){if(e.code==='ENOENT')return null;throw e;}}
  async byRevision(revision){try{return JSON.parse(await readFile(path.join(this.directory,'revisions',revision+'.json'),'utf8'));}catch(e){if(e.code==='ENOENT')return null;throw e;}}
  async history(before,limit){let files;try{files=await readdir(path.join(this.directory,'revisions'));}catch(e){if(e.code==='ENOENT')return [];throw e;}const rows=await Promise.all(files.filter(f=>/^[a-f0-9]{64}\.json$/.test(f)).map(f=>this.byRevision(f.slice(0,-5))));return rows.filter(r=>r&&(!before||r.generated_at<before)).sort((a,b)=>b.generated_at.localeCompare(a.generated_at)).slice(0,limit).map(({revision,generated_at,received_at})=>({revision,generated_at,received_at}));}
  put(row){const work=this.queue.then(async()=>{const old=await this.latest();if(old?.generated_at>row.generated_at)return 'stale';if(old?.generated_at===row.generated_at)return old.revision===row.revision?'duplicate':'conflict';await mkdir(path.join(this.directory,'revisions'),{recursive:true});const revisionFile=path.join(this.directory,'revisions',row.revision+'.json');try{await writeFile(revisionFile,JSON.stringify(row),{flag:'wx'});}catch(e){if(e.code!=='EEXIST')throw e;const saved=JSON.parse(await readFile(revisionFile,'utf8'));if(saved.revision!==row.revision||saved.payload!==row.payload)throw new Error('Immutable revision mismatch.');}const temp=path.join(this.directory,'current.tmp');await writeFile(temp,JSON.stringify(row));await rename(temp,path.join(this.directory,'current.json'));return 'accepted';});this.queue=work.catch(()=>{});return work;}
}
export function createServer({directory=path.join(here,'.local-data'),token=process.env.INGEST_TOKEN,demo=false}={}){
  const store=new FileStore(directory);
  return http.createServer(async(req,res)=>{
    try{
      // Local development is loopback-only and rejects browser-origin writes.
      const host=req.headers.host||'localhost';if(!/^(localhost|127\.0\.0\.1|\[::1\])(?::\d+)?$/.test(host)){res.writeHead(403);res.end('Loopback host required.');return;}
      const url=new URL(req.url,'http://'+host);
      if(req.method==='POST'&&req.headers.origin&&req.headers.origin!==url.origin){res.writeHead(403);res.end('Cross-origin writes rejected.');return;}
      let body=null;if(!['GET','HEAD'].includes(req.method)){let size=0;const chunks=[];for await(const c of req){size+=c.length;if(size>MAX_BODY){res.writeHead(413);res.end('Snapshot too large.');return;}chunks.push(c);}body=Buffer.concat(chunks);}
      let response;if(url.pathname.startsWith('/api/'))response=await handleAPI(new Request(url,{method:req.method,headers:req.headers,...(body?{body}:{} )}),{LOCAL_PREVIEW:true,INGEST_TOKEN:token},store);
      else{const files={'/':'index.html','/index.html':'index.html','/style.css':'style.css','/app.js':'app.js','/presentation.mjs':'presentation.mjs'};let file=files[url.pathname];if(url.pathname==='/demo.json'&&demo)file='demo.json';if(!file){res.writeHead(404);res.end('Not found.');return;}if(!['GET','HEAD'].includes(req.method)){res.writeHead(405);res.end('Use GET.');return;}const data=await readFile(path.join(here,'public',file));const type=file.endsWith('.css')?'text/css':file.endsWith('.js')||file.endsWith('.mjs')?'application/javascript':file.endsWith('.json')?'application/json':'text/html';response=new Response(data,{headers:{'content-type':type+'; charset=utf-8','cache-control':'no-store'}});}
      const headers=Object.fromEntries(response.headers);headers['content-security-policy']="default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'self'";headers['referrer-policy']='no-referrer';headers['x-content-type-options']='nosniff';res.writeHead(response.status,headers);res.end(req.method==='HEAD'?undefined:Buffer.from(await response.arrayBuffer()));
    }catch(error){console.error('Request failed:',error.message);res.writeHead(500,{'content-type':'application/json'});res.end(JSON.stringify({error:'The dashboard could not complete this request.'}));}
  });
}
if(process.argv[1]&&path.resolve(process.argv[1])===fileURLToPath(import.meta.url)){const port=Number(process.env.PORT||8787);const server=createServer({demo:process.argv.includes('--demo')});server.listen(port,'127.0.0.1',()=>{console.log(`FTSE dashboard: http://127.0.0.1:${port}${process.argv.includes('--demo')?'/?demo=1':''}`);if(!process.env.INGEST_TOKEN)console.log('Ingestion disabled. Set INGEST_TOKEN to enable authenticated test updates.');});}
