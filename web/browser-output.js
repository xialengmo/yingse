'use strict';

// The browser supplies an authorized directory handle, never an absolute path.
// Completed server renders are streamed into that handle without buffering video.
class BrowserOutput {
  constructor(changed, acknowledged) {
    this.changed=changed;this.acknowledged=acknowledged;
    this.current=null;this.records=new Map();this.transfers=new Map();this.running=false;
    this.database=new Promise(resolve=>{
      try {
        const request=indexedDB.open('action4-browser-output',1);
        request.onupgradeneeded=()=>request.result.createObjectStore('values');
        request.onsuccess=()=>resolve(request.result);request.onerror=()=>resolve(null);
      }catch{resolve(null);}
    });
    this.ready=this.restore();
  }
  async get(key) {
    if(this.records.has(key))return this.records.get(key);
    const db=await this.database;if(!db)return undefined;
    return new Promise(resolve=>{
      const request=db.transaction('values','readonly').objectStore('values').get(key);
      request.onsuccess=()=>{this.records.set(key,request.result);resolve(request.result);};
      request.onerror=()=>resolve(undefined);
    });
  }
  async put(key,value) {
    this.records.set(key,value);
    const db=await this.database;if(!db)return;
    await new Promise(resolve=>{
      const transaction=db.transaction('values','readwrite');
      try{transaction.objectStore('values').put(value,key);}catch{resolve();return;}
      transaction.oncomplete=transaction.onerror=transaction.onabort=()=>resolve();
    });
  }
  async restore() {
    const id=await this.get('active');
    if(id)this.current=await this.get('directory:'+id)||null;
    if(this.current)try{this.current.permission=await this.current.handle.queryPermission({mode:'readwrite'});}catch{this.current.permission='prompt';}
    this.changed();
  }
  async choose() {
    if(typeof window.showDirectoryPicker!=='function')throw new Error('请用 Chrome 或 Edge 打开工作台，选择保存文件夹。');
    // This must be the first awaited operation following the user's click.
    const handle=await window.showDirectoryPicker({id:'action4-save',mode:'readwrite'});
    const ids=await this.get('directories')||[];
    let id=null;
    for(const candidate of ids) {
      const record=await this.get('directory:'+candidate);
      try{if(record&&await record.handle.isSameEntry(handle)){id=candidate;break;}}catch{}
    }
    if(!id){id=crypto.randomUUID();ids.push(id);}
    this.current={id,name:handle.name,handle,permission:'granted'};
    await this.put('directory:'+id,this.current);await this.put('directories',ids);await this.put('active',id);
    for(const [job,transfer] of this.transfers)if(transfer.status!=='saved')this.transfers.delete(job);
    this.changed();return this.current;
  }
  async target() {
    await this.ready;
    if(!this.current)throw new Error('请先选择保存文件夹。');
    if(await this.current.handle.queryPermission({mode:'readwrite'})!=='granted')throw new Error('请点击“选择文件夹”，重新授权保存位置。');
    return {id:this.current.id,name:this.current.name};
  }
  status(job) {
    if(!job.browser_target)return null;
    if(job.browser_saved)return {status:'saved',progress:1};
    return this.transfers.get(job.id)||{status:'waiting',progress:0};
  }
  retry(job) {this.transfers.delete(job.id);}
  async flush(jobs) {
    if(this.running)return;
    this.running=true;
    try {
      await this.ready;
      for(const job of jobs) {
        if(job.status!=='done'||!job.browser_target||job.browser_saved)continue;
        if(this.transfers.has(job.id))continue;
        const done=await this.get('saved:'+job.id);
        if(done){await this.acknowledged(job,done.name,done.bytes||job.bytes);continue;}
        const directory=await this.get('directory:'+job.browser_target);
        if(!directory||await directory.handle.queryPermission({mode:'readwrite'})!=='granted') {
          this.transfers.set(job.id,{status:'waiting',progress:0});this.changed();continue;
        }
        const transfer={status:'saving',progress:0};this.transfers.set(job.id,transfer);this.changed();
        let writer,reader;
        try {
          const response=await fetch('/api/download/'+job.id);
          if(!response.ok||!response.body)throw new Error('读取成片失败，请重试保存。');
          const name=job.filename||job.output.split(/[\\/]/).pop();
          const file=await directory.handle.getFileHandle(name,{create:true});
          writer=await file.createWritable();reader=response.body.getReader();
          const total=Number(response.headers.get('Content-Length'))||job.bytes||0;
          let count=0,last=0;
          while(true) {
            const chunk=await reader.read();if(chunk.done)break;
            await writer.write(chunk.value);count+=chunk.value.byteLength;
            transfer.progress=total?Math.min(.99,count/total):0;
            if(performance.now()-last>400){last=performance.now();this.changed();}
          }
          if(total&&count!==total)throw new Error('成片传输未完成，请重试保存。');
          await writer.close();writer=null;
          await this.put('saved:'+job.id,{name,target:directory.id,bytes:count});
          await this.acknowledged(job,name,count);
          transfer.status='saved';transfer.progress=1;
        } catch(error) {
          if(writer)try{await writer.abort();}catch{}
          transfer.status='failed';transfer.error=error.message||'保存失败，请重新选择文件夹后重试。';
        } finally {if(reader)reader.releaseLock();this.changed();}
      }
    } finally {this.running=false;}
  }
}
window.BrowserOutput=BrowserOutput;
