'use strict';
// Offline Chrome / CDP pipe tests. No npm dependencies, remote debugging port,
// real authenticator, real credentials, or external market/broker connections.
const {spawn}=require('node:child_process');
const fs=require('node:fs');
const path=require('node:path');
const readline=require('node:readline');
const assert=require('node:assert/strict');

async function main(config,lines){
  const chrome=spawn(config.chrome,['--headless=new','--no-first-run','--no-default-browser-check','--disable-background-networking',
    '--disable-component-update','--disable-sync','--disable-default-apps','--disable-extensions','--metrics-recording-only',
    '--disable-domain-reliability','--disable-features=MediaRouter,OptimizationHints,AutofillServerCommunication',
    '--host-resolver-rules=MAP * ~NOTFOUND, EXCLUDE localhost','--ignore-certificate-errors',
    '--password-store=basic','--remote-debugging-pipe','--user-data-dir='+config.profile,'about:blank'],
    {stdio:['ignore','ignore','pipe','pipe','pipe']});
  let serial=0,buffer=Buffer.alloc(0),sessionId;const pending=new Map();const errors=[];const csp=[];const posts=[];const denials=[];
  let holdHistory=false,heldHistory=null;
  let holdKeyBootstrap=false,heldKeyBootstrap=null;
  let primaryAuthenticator;
  chrome.stderr.on('data',()=>{}); // Chrome diagnostics must not print test secrets.
  const send=(method,params={},session=sessionId)=>new Promise((resolve,reject)=>{
    const id=++serial;const timer=setTimeout(()=>{pending.delete(id);reject(Error('CDP timeout: '+method));},15000);
    pending.set(id,{resolve,reject,timer});chrome.stdio[3].write(JSON.stringify({id,method,params,...(session?{sessionId:session}:{})})+'\0');
  });
  chrome.on('exit',(code)=>{for(const item of pending.values()){clearTimeout(item.timer);item.reject(Error('Chrome exited: '+code));}pending.clear();});
  chrome.stdio[4].on('data',chunk=>{
    buffer=Buffer.concat([buffer,chunk]);let stop;
    while((stop=buffer.indexOf(0))>=0){const raw=buffer.subarray(0,stop).toString();buffer=buffer.subarray(stop+1);if(!raw)continue;
      const message=JSON.parse(raw);
      if(message.id){const item=pending.get(message.id);if(item){pending.delete(message.id);clearTimeout(item.timer);message.error?item.reject(Error(message.error.message)):item.resolve(message.result);}continue;}
      if(message.method==='Runtime.exceptionThrown')errors.push(message.params.exceptionDetails.text);
      if(message.method==='Log.entryAdded'&&message.params.entry.source==='security')csp.push(message.params.entry.text);
      if(message.method==='Network.requestWillBeSent'&&message.params.request.method==='POST')posts.push(new URL(message.params.request.url).pathname);
      if(message.method==='Network.responseReceived'&&message.params.response.status>=400){
        const entry={path:new URL(message.params.response.url).pathname,status:message.params.response.status};denials.push(entry);
        send('Network.getResponseBody',{requestId:message.params.requestId}).then(result=>{
          try{const code=JSON.parse(result.body).error;if(typeof code==='string'&&/^[A-Z0-9_]+$/.test(code))entry.code=code;}catch{}
        }).catch(()=>{});
      }
      if(message.method==='Fetch.requestPaused'){
        const request=message.params.request;
        if(message.params.responseStatusCode&&holdKeyBootstrap&&new URL(request.url).pathname==='/auth/session'){
          heldKeyBootstrap=message.params.requestId;holdKeyBootstrap=false;continue;
        }
        if(message.params.responseStatusCode&&holdHistory&&new URL(request.url).pathname.match(/^\/api\/history\/[0-9a-f-]+$/)){
          heldHistory=message.params.requestId;holdHistory=false;continue;
        }
        if(request.url.startsWith(config.origin+'/'))send('Fetch.continueRequest',{requestId:message.params.requestId}).catch(()=>{});
        else send('Fetch.failRequest',{requestId:message.params.requestId,errorReason:'BlockedByClient'}).catch(()=>{});
      }
    }
  });
  const evaluate=async expression=>{
    const result=await send('Runtime.evaluate',{expression,returnByValue:true,awaitPromise:true});
    if(result.exceptionDetails)throw Error(result.exceptionDetails.exception?.description||'Browser evaluation failed');return result.result.value;
  };
  async function wait(expression){for(let n=0;n<300;n++){if(await evaluate(expression))return;await new Promise(r=>setTimeout(r,50));}
    const diagnostic=await evaluate('({notice:document.getElementById("notice")?.textContent,authentication:document.getElementById("auth-status")?.textContent,nativeError:window.__testAuthError})');
    throw Error('UI condition timed out: '+expression+' · '+JSON.stringify(diagnostic)+' · '+JSON.stringify(denials.slice(-8)));}
  async function click(id){const point=await evaluate(`(()=>{const e=document.getElementById(${JSON.stringify(id)});e.scrollIntoView({block:'center'});const r=e.getBoundingClientRect();if(!r.width||!r.height||e.disabled)throw Error('Control unavailable');return {x:r.x+r.width/2,y:r.y+r.height/2};})()`);
    await send('Input.dispatchMouseEvent',{type:'mousePressed',...point,button:'left',clickCount:1});await send('Input.dispatchMouseEvent',{type:'mouseReleased',...point,button:'left',clickCount:1});}
  async function parent(event){process.stdout.write(JSON.stringify({event})+'\n');const response=await lines.next();assert.equal(JSON.parse(response.value).ok,true);}
  async function accessibility(stage){
    const {nodes}=await send('Accessibility.getFullAXTree');
    const exposed=nodes.filter(n=>!n.ignored);
    assert(exposed.some(n=>n.role?.value==='main'),stage+': missing main landmark');
    for(const node of exposed.filter(n=>['button','textbox','combobox','searchbox'].includes(n.role?.value))){
      assert(node.name?.value?.trim(),stage+': unnamed '+node.role.value);
    }
    const trading=exposed.filter(n=>n.role?.value==='button'&&/^(CALL|PUT|EXIT ALL)(\b|$)/.test(n.name?.value||''));
    assert.equal(trading.length,stage==='signed-out'?1:3,stage+': unexpected accessible trading controls');
    if(stage==='signed-out')assert.equal(trading[0].name.value,'EXIT ALL');
  }
  async function screenshot(name,width=1440,height=1080){await send('Emulation.setDeviceMetricsOverride',{width,height,deviceScaleFactor:1,mobile:false});
    await evaluate('window.scrollTo(0,0)');
    const shot=await send('Page.captureScreenshot',{format:'png',captureBeyondViewport:true});fs.writeFileSync(path.join(config.screenshots,name+'.png'),Buffer.from(shot.data,'base64'));
    assert(await evaluate('document.documentElement.scrollWidth <= innerWidth+1'),'Layout overflow at '+width);
  }
  try {
    await send('Browser.getVersion',{},undefined);
    const target=await send('Target.createTarget',{url:'about:blank'},undefined);
    sessionId=(await send('Target.attachToTarget',{targetId:target.targetId,flatten:true},undefined)).sessionId;
    await send('Page.enable');await send('Runtime.enable');await send('Log.enable');await send('Network.enable');
    await send('Fetch.enable',{patterns:[{urlPattern:'*',requestStage:'Request'},{urlPattern:config.origin+'/api/history/*',requestStage:'Response'},
      {urlPattern:config.origin+'/auth/session',requestStage:'Response'}]});
    await send('WebAuthn.enable');
    primaryAuthenticator=(await send('WebAuthn.addVirtualAuthenticator',{options:{protocol:'ctap2',transport:'usb',hasResidentKey:true,hasUserVerification:true,isUserVerified:true,automaticPresenceSimulation:true}})).authenticatorId;
    await send('Page.navigate',{url:config.origin+'/dashboard'});
    await wait('document.getElementById("login") && document.getElementById("auth-status").textContent.includes("Sign in")');
    assert.equal(await evaluate('document.querySelectorAll("[data-trading-action]").length'),3);
    assert.equal(await evaluate('document.getElementById("private-workspace").hidden'),true);
    await accessibility('signed-out');
    await screenshot(config.scenario+'-login');
    await evaluate('document.getElementById("enrollment").open=true');
    await evaluate('document.getElementById("bootstrap").value='+JSON.stringify(config.bootstrap));config.bootstrap='';
    await click('enroll');await wait('document.getElementById("auth-status").textContent.includes("Passkey registered")');
    await click('login');await wait('!document.getElementById("private-workspace").hidden');
    assert.equal(await evaluate('document.getElementById("call").disabled'),true,'Login must not authorize trading');
    await evaluate('document.querySelector("[data-page=security]").click()');
    await wait('!document.getElementById("authorize").disabled');
    await click('authorize');await wait('!document.getElementById("call").disabled');
    await screenshot(config.scenario+'-security');
    if(config.scenario==='recovery'){
      // Pause only the browser registration call so we can swap virtual hardware.
      // All challenges, assertions, signatures and server verification remain real.
      await evaluate(`window.__nativeCreate=navigator.credentials.create.bind(navigator.credentials);
        navigator.credentials.create=options=>new Promise((resolve,reject)=>{window.__pendingRegistration={options,resolve,reject};});
        window.__nativeGet=navigator.credentials.get.bind(navigator.credentials);
        navigator.credentials.get=async options=>{try{return await window.__nativeGet(options);}catch(error){window.__testAuthError=error.name;throw error;}};`);
      async function registerWithAnotherKey(previous){
        await wait('Boolean(window.__pendingRegistration)');
        const previousMessage=await evaluate('document.getElementById("auth-status").textContent');
        await send('WebAuthn.setAutomaticPresenceSimulation',{authenticatorId:previous,enabled:false});
        const next=(await send('WebAuthn.addVirtualAuthenticator',{options:{protocol:'ctap2',transport:'usb',hasResidentKey:true,hasUserVerification:true,isUserVerified:true,automaticPresenceSimulation:true}})).authenticatorId;
        await evaluate(`(()=>{const p=window.__pendingRegistration;delete window.__pendingRegistration;window.__nativeCreate(p.options).then(p.resolve,p.reject);})()`);
        await wait('document.getElementById("private-workspace").hidden && document.getElementById("auth-status").textContent !== '+JSON.stringify(previousMessage));
        assert.equal(await evaluate('(async()=>{const r=await fetch("/api/status");return r.status;})()'),401);
        // Physical custody changes in this scenario: the preceding device is
        // lost or put away, rather than still plugged in with presence disabled.
        await send('WebAuthn.removeVirtualAuthenticator',{authenticatorId:previous});
        return next;
      }
      await click('add-recovery-key');const recovery=await registerWithAnotherKey(primaryAuthenticator);
      await click('recover-key');const replacement=await registerWithAnotherKey(recovery);
      await click('login');await wait('!document.getElementById("private-workspace").hidden');
      assert.equal(await evaluate('document.getElementById("call").disabled'),true,'Recovery must not authorize trading');
      await evaluate('document.querySelector("[data-page=security]").click()');
      const credential=(await send('WebAuthn.getCredentials',{authenticatorId:replacement})).credentials[0].credentialId;
      await click('add-login-key');const backup=await registerWithAnotherKey(replacement);
      await click('login');await wait('!document.getElementById("private-workspace").hidden');
      await evaluate('document.querySelector("[data-page=security]").click()');
      const target=Buffer.from(credential,'base64').toString('base64url');
      await wait(`Array.from(document.getElementById('credential-target').options).some(o=>o.value===${JSON.stringify(target)})`);
      await evaluate('document.getElementById("credential-target").value='+JSON.stringify(target)+';document.getElementById("credential-target").dispatchEvent(new Event("change"))');
      holdKeyBootstrap=true;await click('revoke-key');
      for(let n=0;n<100&&!heldKeyBootstrap;n++)await new Promise(r=>setTimeout(r,20));
      assert(heldKeyBootstrap,'Revocation session refresh must be paused');
      await evaluate('document.getElementById("credential-target").value=""');
      await send('Fetch.continueRequest',{requestId:heldKeyBootstrap});
      await wait('document.getElementById("private-workspace").hidden');
      await click('login');await wait('!document.getElementById("private-workspace").hidden');
      const keys=await evaluate('(async()=>{const r=await fetch("/auth/credentials");return (await r.json()).credentials;})()');
      assert(!keys.some(k=>k.credential_id===target),'Compromised login key survived revocation');
      assert(keys.some(k=>k.role==='RECOVERY'));
      assert.equal(posts.filter(p=>p==='/api/commands').length,0,'Key recovery submitted an entry');
      assert.equal(errors.length,0,errors.join('\n'));
      await screenshot('recovered-security');
      process.stdout.write(JSON.stringify({event:'done',assertions:'real browser recovery-only key, replacement, no auto authority, backup enrollment and distinct-key revocation'})+'\n');
      return;
    }
    await evaluate('document.querySelector("[data-page=dashboard]").click()');
    await accessibility('dashboard');
    await evaluate('document.getElementById("call").focus()');
    await send('Input.dispatchKeyEvent',{type:'keyDown',key:'Tab',code:'Tab',windowsVirtualKeyCode:9});
    await send('Input.dispatchKeyEvent',{type:'keyUp',key:'Tab',code:'Tab',windowsVirtualKeyCode:9});
    assert.equal(await evaluate('document.activeElement.id'),'put','Entry keyboard order');
    const action=config.scenario==='put'?'put':'call';const before=posts.filter(p=>p==='/api/commands').length;
    await evaluate(`document.getElementById('${action}').click(); document.getElementById('${action}').click();`);
    await wait('document.getElementById("planned-total").textContent === "125"');
    assert.equal(posts.filter(p=>p==='/api/commands').length,before+1,'Double-click created duplicate command');
    assert.equal(await evaluate('document.querySelectorAll("#allocation-rows tr").length'),5);
    assert.equal(await evaluate('document.querySelectorAll("#allocation-rows input, #allocation-rows select, #allocation-rows button").length'),0);
    await parent('fill');await evaluate('new Promise(r=>setTimeout(r,100))');
    await wait('document.getElementById("position-quantity").textContent === "5"');
    assert.equal(await evaluate('document.getElementById("finalized-result").hidden'),true);
    assert.equal(await evaluate('document.body.innerText.includes("Unrealized")'),false);
    await screenshot(config.scenario+'-desktop');await screenshot(config.scenario+'-tablet',900,1180);
    if(config.scenario==='call'){
      await evaluate('document.querySelector("[data-page=history]").click()');
      await wait('document.getElementById("history-page-info").textContent.includes("matching")');
      await evaluate('document.getElementById("history-state").value="PENDING_RECONCILIATION";document.getElementById("history-filters").requestSubmit()');
      await wait('document.querySelector("#history-rows .row-link") !== null');
      await evaluate('document.querySelector("#history-rows .row-link").click()');
      await wait('!document.getElementById("trade-details").hidden');
      assert(await evaluate('document.getElementById("detail-facts").innerText.includes("PENDING RECONCILIATION")'));
      assert.equal(await evaluate('document.getElementById("strike-detail-container").hidden'),true);
      assert(await evaluate('document.getElementById("chart").innerText.includes("withheld")'));
      assert(await evaluate('!document.getElementById("exit-all").disabled && document.getElementById("exit-all").closest("[data-screen]") === null'));
      await screenshot('pending-reconciliation',1440,1080);
      await evaluate('document.getElementById("history-state").value="FINALIZED";document.querySelector("[data-page=dashboard]").click()');
    }
    const count=posts.filter(p=>p==='/api/commands').length;
    await send('Page.reload');await wait('!document.getElementById("private-workspace").hidden && document.getElementById("planned-total").textContent === "125"');
    assert.equal(posts.filter(p=>p==='/api/commands').length,count,'Refresh replayed a trade command');
    if(config.scenario==='put'){
      await parent('expire');
      await wait('document.getElementById("private-workspace").hidden');
      assert.equal(await evaluate('document.getElementById("history-rows").children.length'),0);
      assert.equal(await evaluate('document.getElementById("call").disabled'),true);
      await screenshot('expired-session');
    } else {
      await parent('strategy-close');
      await wait('!document.getElementById("put").disabled');
      await click('put');await wait('document.getElementById("position-direction").textContent === "PUT"');
      await parent('fill');await wait('document.getElementById("position-quantity").textContent === "5"');
      await click('exit-all');await wait('document.getElementById("emergency-status").textContent.includes("LATCHED")');
      await parent('close');
      await evaluate('document.querySelector("[data-page=history]").click()');
      await wait('document.querySelector("#history-rows .row-link") !== null');
      assert.equal(await evaluate('document.getElementById("summary-count").textContent'),'2');
      await evaluate('document.querySelector("#history-rows .row-link").click()');
      await wait('!document.getElementById("trade-details").hidden');
      assert.equal(await evaluate('document.querySelectorAll("#strike-detail-rows tr").length'),5);
      assert(await evaluate('document.getElementById("chart").innerText.includes("Chart unavailable")'));
      await screenshot('history-desktop',1440,1200);
      // History has only the global emergency action; the dashboard entries are hidden.
      const historyAX=await send('Accessibility.getFullAXTree');
      assert(historyAX.nodes.some(n=>!n.ignored&&n.role?.value==='region'&&n.name?.value==='Per-strike realized results'));
      for(const node of historyAX.nodes.filter(n=>!n.ignored&&['textbox','combobox','searchbox'].includes(n.role?.value)))assert(node.name?.value?.trim(),'Unnamed history filter');
      await evaluate('document.getElementById("history-page-size").value="1";document.getElementById("history-page-size").dispatchEvent(new Event("change"))');
      await wait('!document.getElementById("next-page").disabled');
      await click('next-page');await wait('document.getElementById("history-page-info").textContent.includes("Page 2")');
      assert(await evaluate('document.getElementById("history-rows").innerText.includes("CALL")'));
      await evaluate('document.querySelector("#history-rows .row-link").click()');
      await wait('document.querySelector("#chart svg") !== null');
      assert(await evaluate('document.querySelectorAll("#chart .price-point").length >= 2'));
      assert.equal(await evaluate('document.querySelectorAll("#chart svg path").length'),0,'No invented candle or interpolated path');
      await screenshot('history-recorded-chart',1440,1200);
      await evaluate('document.getElementById("history-direction").value="PUT";document.getElementById("history-filters").requestSubmit()');
      await wait('document.getElementById("history-page-info").textContent.includes("1 matching")');
      await evaluate('document.getElementById("history-direction").value="";document.getElementById("history-state").value="PENDING_RECONCILIATION";document.getElementById("history-filters").requestSubmit()');
      await wait('document.getElementById("history-page-info").textContent.includes("0 matching")');
      await evaluate('document.getElementById("history-state").value="FINALIZED";document.getElementById("history-filters").requestSubmit()');
      await wait('document.querySelector("#history-rows .row-link") !== null');
      holdHistory=true;await evaluate('document.querySelector("#history-rows .row-link").click()');
      for(let n=0;n<100&&!heldHistory;n++)await new Promise(r=>setTimeout(r,20));
      assert(heldHistory,'A history response must be paused for the logout race');
      await click('logout');await wait('document.getElementById("private-workspace").hidden');
      await wait('document.getElementById("notice").textContent.startsWith("Signed out.")');
      await send('Fetch.continueRequest',{requestId:heldHistory});await new Promise(r=>setTimeout(r,100));
      assert.equal(await evaluate('document.getElementById("strike-detail-rows").children.length'),0,'Late private response repainted signed-out content');
      assert.equal(await evaluate('(async()=>{const r=await fetch("/api/status",{credentials:"same-origin"});return r.status;})()'),401);
    }
    assert.equal(errors.length,0,errors.join('\n'));
    assert.equal(csp.filter(s=>s.includes('violates')||s.includes('Refused')).length,0,csp.join('\n'));
    process.stdout.write(JSON.stringify({event:'done',assertions:'passkey, authorization, three actions, one-press, double-click, allocation, blind mode, refresh, history, emergency, expiry, responsive'})+'\n');
  } catch(error) {process.stderr.write('BROWSER ASSERTION: '+String(error.stack)+'\n');throw error;}
  finally {try{await Promise.race([send('Browser.close',{},undefined),new Promise(r=>setTimeout(r,1000))]);}catch{}chrome.kill('SIGKILL');}
}
const reader=readline.createInterface({input:process.stdin});const lines=reader[Symbol.asyncIterator]();
(async()=>{const config=JSON.parse((await lines.next()).value);await main(config,lines);reader.close();process.exit(0);})().catch(error=>{process.stderr.write(String(error.stack)+'\n');reader.close();process.exit(1);});
