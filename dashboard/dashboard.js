/* Mortificatio browser presentation. Only sanitized authenticated APIs are used. */
(function () {
  'use strict';
  const $ = id => document.getElementById(id);
  const state = {authenticated:false, csrf:null, session:null, status:null, allocation:null, scope:null,
    busy:false, emergencyBusy:false, emergencyRequested:false, envelope:null, generation:0, historyGeneration:0, page:1, pageSize:10,
    history:null, selectedTrade:null, pageName:'dashboard', refreshing:false, pollCount:0,sessionReceived:0,
    detailRevisions:new Map(),finalizedIds:new Set()};
  const UUID=/^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/;
  const labels = {FINALIZED:'Finalized',PENDING_RECONCILIATION:'Pending reconciliation',CANCELLED_NO_FILL:'Canceled · no fill',
    ACQUIRING:'Acquiring',HOLDING:'Holding',EXITING:'Liquidation pending',FLAT_CONFIRMED:'Broker-confirmed flat',
    ACQUISITION_HALTED:'Acquisition halted',RECOVERY_REQUIRED:'Recovery required',OPEN_OFFLINE:'Open · simulation'};
  const pageNames=new Set(['dashboard','history','security']);
  function text(id,value) {const next=value===null||value===undefined?'—':String(value);if($(id).textContent!==next)$(id).textContent=next;}
  function label(value) {return labels[value]||String(value||'Unavailable').replaceAll('_',' ').toLowerCase();}
  function notify(message) {$('notice').hidden=!message;text('notice',message||'');}
  function cell(value) {const td=document.createElement('td');td.textContent=value===null||value===undefined?'—':String(value);return td;}
  function dateTime(value) {if(!value)return '—';const date=new Date(value);return Number.isFinite(date.valueOf())?new Intl.DateTimeFormat('en-US',{timeZone:'America/New_York',month:'short',day:'2-digit',hour:'2-digit',minute:'2-digit',second:'2-digit',hour12:false}).format(date)+' ET':'Unavailable';}
  function decimal(value,currency=false) {
    if(value===null||value===undefined)return '—';
    const match=String(value).match(/^(-?)(\d+)(?:\.(\d+))?$/);if(!match)return 'Unavailable';
    const integral=BigInt(match[2]).toLocaleString('en-US');const fraction=(match[3]||'').replace(/0+$/,'').padEnd(2,'0');
    return match[1]+(currency?'$':'')+integral+'.'+fraction;
  }
  function money(value) {return decimal(value,true);}
  function duration(value) {if(value===null||value===undefined)return '—';const seconds=Number(value);if(!Number.isFinite(seconds)||seconds<0)return 'Unavailable';return seconds<60?seconds.toFixed(1)+'s':Math.floor(seconds/60)+'m '+Math.floor(seconds%60)+'s';}
  function authorizationNow() {return state.session?.server_time+(performance.now()-state.sessionReceived)/1000;}
  function errorText(error) {
    const code=error.code||'';
    if(error.status===401)return 'Authentication expired or was revoked. Independent protection continues. Sign in again to regain dashboard and emergency access.';
    if(code.includes('CSRF'))return 'Your security context changed in another tab. Refresh your session and try the same action again.';
    if(code.includes('AUTHORIZATION'))return 'Trading authorization is unavailable or expired. Establish a fresh authorization in Security.';
    if(code.includes('BROKER')||code.includes('RECONCILIATION'))return 'Trading is unavailable until broker evidence and reconciliation pass. No fill or liquidation is implied.';
    if(error.status===429)return 'Request limit reached. Wait before refreshing history or security access.';
    if(error.name==='AbortError'||error.name==='TypeError')return 'The response is uncertain. Check reconciliation before acting again. No trade command will be replayed automatically.';
    return 'The request could not be completed safely. Check system health and reconciliation; no result is assumed.';
  }
  async function request(path,{method='GET',body,bootstrap=false,authorization,csrf}={}) {
    const controller=new AbortController();const timeout=setTimeout(()=>controller.abort(),8000);
    const headers={Accept:'application/json'};
    if(bootstrap)headers['X-Mortificatio-Session']='bootstrap';
    if(method==='POST'){headers['Content-Type']='application/json';if(csrf||state.csrf)headers['X-CSRF-Token']=csrf||state.csrf;}
    if(authorization)headers.Authorization=authorization;
    try {
      const response=await fetch(path,{method,headers,body:method==='POST'?JSON.stringify(body||{}):undefined,
        credentials:'same-origin',cache:'no-store',redirect:'error',signal:controller.signal});
      const data=await response.json();
      if(!response.ok){const error=new Error('Request rejected');error.code=data.error||'REQUEST_REJECTED';error.status=response.status;throw error;}
      return data;
    } finally {clearTimeout(timeout);}
  }
  function authLost(message) {
    state.authenticated=false;state.csrf=null;state.session=null;state.status=null;state.allocation=null;state.scope=null;
    state.history=null;state.selectedTrade=null;state.generation++;state.historyGeneration++;state.refreshing=false;
    state.detailRevisions.clear();state.finalizedIds.clear();
    $('auth-screen').hidden=false;$('private-workspace').hidden=true;$('logout').hidden=true;
    $('account-recovery').hidden=false;$('key-management').hidden=true;$('credential-target').replaceChildren();
    $('history-rows').replaceChildren();$('allocation-rows').replaceChildren();$('strike-detail-rows').replaceChildren();
    $('detail-facts').replaceChildren();$('chart').replaceChildren();$('execution-records').replaceChildren();
    $('trade-details').hidden=true;$('finalized-result').hidden=true;
    for(const id of ['summary-count','summary-win','summary-total','summary-average','result-net'])text(id,'—');
    for(const id of ['position-state','position-direction','position-quantity','protection-status','reconciliation-status','emergency-status',
      'planned-total','details-title','details-state','security-session','security-grant','security-broker','broker-status','authorization-status'])text(id,'—');
    for(const id of ['history-search','history-direction','history-start','history-end','bootstrap'])$(id).value='';
    $('history-state').value='FINALIZED';$('history-page-size').value='10';state.page=1;state.pageSize=10;
    text('summary-wins','Undefined without completed trades');text('history-page-info','No private records loaded');
    $('result-history-link').onclick=null;
    text('auth-title','Secure access.');text('auth-status',message||'Sign in with your passkey.');notify(message||'');
    setButtons();
  }
  function handleError(error) {if(error.status===401)authLost(errorText(error));else notify(errorText(error));}
  function readEnvelope() {
    if(!state.scope)return null;
    const raw=localStorage.getItem('mortificatio-command:'+state.scope);if(!raw)return null;
    try {const value=JSON.parse(raw);if(!UUID.test(value.command_id)||!['CALL','PUT'].includes(value.direction)||!Number.isSafeInteger(value.entry_epoch)||value.entry_epoch<0||Object.keys(value).some(k=>!['command_id','direction','entry_epoch','trade_id','phase'].includes(k)))throw Error();return value;}
    catch {notify('Local command metadata is invalid. Trading stays blocked until the session is reconciled.');return {invalid:true};}
  }
  function saveEnvelope(value) {
    // Only non-secret idempotency metadata. Never tokens, prices, P&L or credentials.
    localStorage.setItem('mortificatio-command:'+state.scope,JSON.stringify(value));state.envelope=value;
  }
  function clearEnvelope() {if(state.scope)localStorage.removeItem('mortificatio-command:'+state.scope);state.envelope=null;}
  async function securityLock(action) {
    if(navigator.locks)return navigator.locks.request('mortificatio-session-mutation',action);
    return action(); // Backend CSRF, UUID, epoch and account reservations remain authoritative.
  }
  async function resume() {
    const generation=state.generation;const session=await request('/auth/session',{bootstrap:true});
    if(generation!==state.generation)return false;
    state.session=session;state.csrf=session.csrf_token;state.scope=session.command_scope;state.authenticated=true;
    state.sessionReceived=performance.now();
    try{state.emergencyRequested=Boolean(localStorage.getItem('mortificatio-emergency:'+state.scope));}catch{state.emergencyRequested=true;}
    state.envelope=readEnvelope();$('auth-screen').hidden=true;$('private-workspace').hidden=false;$('logout').hidden=false;
    $('account-recovery').hidden=true;$('key-management').hidden=state.pageName!=='security';
    renderSecurity();setButtons();return true;
  }
  function bytes(value) {const raw=atob(value.replaceAll('-','+').replaceAll('_','/').padEnd(Math.ceil(value.length/4)*4,'='));return Uint8Array.from(raw,c=>c.charCodeAt(0));}
  function encoded(value) {return btoa(String.fromCharCode(...new Uint8Array(value))).replaceAll('+','-').replaceAll('/','_').replaceAll('=','');}
  function credentialOptions(source,registration=false) {
    const options={...source,challenge:bytes(source.challenge)};
    if(registration)options.user={...source.user,id:bytes(source.user.id)};
    for(const key of ['allowCredentials','excludeCredentials'])if(source[key])options[key]=source[key].map(item=>({...item,id:bytes(item.id)}));
    return options;
  }
  function credentialJSON(credential) {
    const response={clientDataJSON:encoded(credential.response.clientDataJSON)};
    for(const key of ['attestationObject','authenticatorData','signature','userHandle'])
      if(credential.response[key]!==undefined)response[key]=credential.response[key]===null?null:encoded(credential.response[key]);
    if(credential.response.getTransports)response.transports=credential.response.getTransports();
    return {id:credential.id,rawId:encoded(credential.rawId),type:credential.type,response,
      authenticatorAttachment:credential.authenticatorAttachment||undefined,clientExtensionResults:credential.getClientExtensionResults()};
  }
  function requireWebAuthn() {if(!window.isSecureContext||!navigator.credentials||!window.PublicKeyCredential)throw new Error('Secure passkey access unavailable');}
  async function loadKeys(){
    const generation=state.generation;
    try{const data=await request('/auth/credentials');if(generation!==state.generation||!state.authenticated)return;
      const select=$('credential-target');const selected=select.value;select.replaceChildren();
      const placeholder=document.createElement('option');placeholder.value='';placeholder.textContent='Choose a key by fingerprint';select.append(placeholder);
      for(const key of data.credentials){const option=document.createElement('option');option.value=key.credential_id;
        option.textContent=key.role+' · '+key.credential_id;select.append(option);}
      select.value=data.credentials.some(k=>k.credential_id===selected)?selected:'';$('revoke-key').disabled=!select.value;
    }catch(error){if(generation===state.generation)handleError(error);}
  }
  async function keyChange(action){
    const generation=state.generation;const target=$('credential-target').value;
    if(action==='REVOKE'&&!target)return;
    try{await securityLock(async()=>{requireWebAuthn();if(generation!==state.generation||!state.authenticated||!await resume()||generation!==state.generation)return;
      const options=await request('/auth/keys/options',{method:'POST',body:{action,...(action==='REVOKE'?{target}:{})}});
      const proof=await navigator.credentials.get({publicKey:credentialOptions(options.public_key)});
      if(generation!==state.generation)return;
      const result=await request('/auth/keys/prove',{method:'POST',body:{credential:credentialJSON(proof)}});
      if(action!=='REVOKE'){
        const key=await navigator.credentials.create({publicKey:credentialOptions(result.public_key,true)});
        if(generation!==state.generation)return;
        await request('/auth/keys/register',{method:'POST',body:{credential:credentialJSON(key)}});
      }
      authLost('Key change recorded. All dashboard sessions and trading grants were revoked. Sign in again; independent protection continues.');
    });}catch(error){handleError(error);}
  }
  async function recoverKey(){
    $('recover-key').disabled=true;
    try{await securityLock(async()=>{requireWebAuthn();const generation=state.generation;
      const options=await request('/auth/recovery/options',{method:'POST'});
      const proof=await navigator.credentials.get({publicKey:credentialOptions(options.public_key)});
      if(generation!==state.generation)return;
      const registration=await request('/auth/recovery/prove',{method:'POST',body:{credential:credentialJSON(proof)}});
      const replacement=await navigator.credentials.create({publicKey:credentialOptions(registration.public_key,true)});
      if(generation!==state.generation)return;
      await request('/auth/recovery/register',{method:'POST',body:{credential:credentialJSON(replacement)}});
      authLost('Recovery completed. Previous login keys, sessions and trading grants were revoked. Sign in with your replacement key.');
    });}catch(error){handleError(error);}finally{$('recover-key').disabled=false;}
  }
  async function login() {
    $('login').disabled=true;notify('');const generation=state.generation;
    try {requireWebAuthn();await securityLock(async()=>{
      const options=await request('/auth/login/options',{method:'POST'});
      const credential=await navigator.credentials.get({publicKey:credentialOptions(options.public_key)});
      if(generation!==state.generation)return;
      await request('/auth/login/complete',{method:'POST',body:{credential:credentialJSON(credential)}});
      state.generation++;await resume();
    });if(!state.authenticated)return;await refresh(true);navigate(state.pageName,false);}
    catch(error){handleError(error);text('auth-status','Passkey sign-in was not completed. Use a trusted HTTPS origin and your registered key.');}
    finally {$('login').disabled=false;}
  }
  async function enroll() {
    const input=$('bootstrap');const bootstrap=input.value;input.value='';$('enroll').disabled=true;
    try {requireWebAuthn();const options=await request('/auth/register/options',{method:'POST',authorization:'Bootstrap '+bootstrap});
      const credential=await navigator.credentials.create({publicKey:credentialOptions(options.public_key,true)});
      await request('/auth/register/complete',{method:'POST',body:{credential:credentialJSON(credential)}});
      $('enrollment').open=false;text('auth-status','Passkey registered. Sign in to continue. Trading remains unauthorized.');
    }catch(error){handleError(error);}finally{$('enroll').disabled=false;}
  }
  async function authorize() {
    $('authorize').disabled=true;notify('');const generation=state.generation;
    try {await securityLock(async()=>{if(generation!==state.generation||!await resume())return;requireWebAuthn();
      const options=await request('/auth/trading/options',{method:'POST'});
      const credential=await navigator.credentials.get({publicKey:credentialOptions(options.public_key)});
      if(generation!==state.generation)return;
      await request('/auth/trading/complete',{method:'POST',body:{credential:credentialJSON(credential)}});await resume();
    });if(generation!==state.generation)return;notify('Short-lived offline trading authorization established. CALL or PUT now initiates the automatic workflow directly.');await refresh();}
    catch(error){handleError(error);}finally{renderSecurity();setButtons();}
  }
  async function revokeSessions() {
    $('revoke-sessions').disabled=true;
    try {await securityLock(async()=>{await resume();requireWebAuthn();
      const options=await request('/auth/revoke-sessions/options',{method:'POST'});
      const credential=await navigator.credentials.get({publicKey:credentialOptions(options.public_key)});
      await request('/auth/revoke-sessions/complete',{method:'POST',body:{credential:credentialJSON(credential)}});
    });authLost('Dashboard sessions revoked. Independent protection remains authorized.');}
    catch(error){handleError(error);}finally{$('revoke-sessions').disabled=false;}
  }
  async function logout() {
    authLost('Signing out. Independent protection continues for managed exposure.');$('login').disabled=true;
    try {await securityLock(async()=>{const context=await request('/auth/session',{bootstrap:true});
      await request('/auth/logout',{method:'POST',csrf:context.csrf_token});});
      notify('Signed out. Independent protection continues for managed exposure.');
    }catch(error){notify(error.status===401?'Session is no longer authenticated.':'Sign-out response is unconfirmed. Close this private browser session or restore connectivity and sign in again.');}
    finally{$('login').disabled=false;}
  }
  function currentTrade() {const trades=state.status?.lifecycle?.trades||[];return trades.find(t=>!t.reconciled_flat)||trades[trades.length-1]||null;}
  function brokerReady() {const lifecycle=state.status?.lifecycle;return Boolean(state.session?.broker_evidence_available&&
    state.session.broker_evidence_origin==='OFFLINE_FIXTURE'&&lifecycle?.origin==='OFFLINE_FIXTURE'&&
    !lifecycle.entry_fenced&&!String(lifecycle.protective_condition||'').startsWith('CRITICAL')&&
    lifecycle.broker_connected&&lifecycle.account_reconciled&&lifecycle.real_ibkr_authenticated===false&&lifecycle.submission_enabled===false);}
  function entryEligible(direction) {
    if(!state.authenticated||!state.csrf||!state.session?.trading_authorization?.active||!brokerReady()||state.busy||state.emergencyBusy)return false;
    if(!Number.isFinite(authorizationNow())||state.session.trading_authorization.expires_at<=authorizationNow())return false;
    if(state.status?.account_blocked||state.status?.emergency?.latched||state.emergencyRequested||state.envelope?.invalid)return false;
    if(state.envelope)return state.envelope.direction===direction; // Same UUID only; no automatic retry.
    return !(state.status?.pending_commands?.length);
  }
  function setButtons() {
    $('call').disabled=!entryEligible('CALL');$('put').disabled=!entryEligible('PUT');
    // Emergency is independent of entry/grant/broker eligibility and entry busy state.
    $('exit-all').disabled=!state.authenticated||!state.csrf||state.emergencyBusy;
  }
  function renderSecurity() {
    const session=state.session;const grant=session?.trading_authorization;
    const active=Boolean(grant?.active&&grant.expires_at>authorizationNow());
    text('authorization-status',active?'Active · short-lived':'Not established');
    text('security-grant',active?'Active until '+new Intl.DateTimeFormat('en-US',{hour:'2-digit',minute:'2-digit',second:'2-digit'}).format(new Date(grant.expires_at*1000)):'Not established');
    text('security-session',session?'Expires '+new Intl.DateTimeFormat('en-US',{hour:'2-digit',minute:'2-digit'}).format(new Date(session.expires_at*1000)):'Not authenticated');
    const broker=brokerReady()?'Synthetic paper evidence · verified':'Unavailable · broker evidence or reconciliation missing';
    text('broker-status',broker);text('security-broker',broker);
    $('authorize').disabled=!state.authenticated||!brokerReady()||state.busy;
  }
  function renderStatus() {
    if(!state.authenticated)return;
    const data=state.status||{};const trade=currentTrade();const lifecycle=data.lifecycle||{};
    if(trade&&!trade.reconciled_flat)$('finalized-result').hidden=true;
    for(const item of lifecycle.trades||[])if(!item.reconciled_flat&&state.finalizedIds.has(item.trade_id)){
      state.finalizedIds.delete(item.trade_id);
      for(const id of ['summary-count','summary-win','summary-total','summary-average'])text(id,'—');
      $('history-rows').replaceChildren();text('history-page-info','Finalization changed. Current realized results require verification.');
      if(state.selectedTrade===item.trade_id){$('detail-facts').replaceChildren();$('strike-detail-rows').replaceChildren();
        $('execution-records').replaceChildren();renderChart({status:'WITHHELD_BLIND_EXECUTION'});text('details-state','PENDING RECONCILIATION · prior finalization revoked.');}
    }
    text('supervisor-status',lifecycle.supervisor_running?'Independent service running':lifecycle.supervision_mode==='MANUAL_OFFLINE_HARNESS'?'Manual supervision · test mode':'Unavailable');
    text('position-state',trade?label(trade.state):'NO MANAGED TRADE');text('position-direction',trade?.direction);
    text('position-quantity',!lifecycle.account_reconciled?'Unverified · last '+String(trade?.open_contracts??'unknown'):trade?trade.open_contracts:0);
    const protection=trade?.premium_protection||[];
    text('protection-status',protection.length?(protection.every(p=>p.exit_quote_healthy)?'Conditional model · data healthy':'Data unavailable · exposure unresolved'):'No owned protection evidence');
    if(lifecycle.protective_condition&&lifecycle.protective_condition!=='HEALTHY_OFFLINE')text('protection-status',lifecycle.protective_condition+' · exposure requires supervision');
    if(trade?.exit_execution_health?.startsWith('CRITICAL')||trade?.exit_execution_health==='PROTECTED_EXIT_COORDINATION_REQUIRED')
      text('protection-status','Closing execution unresolved · exposure retained');
    text('reconciliation-status',trade?label(trade.reconciliation_state||trade.state):lifecycle.account_reconciled?'Account evidence matched':'Pending reconciliation');
    text('emergency-status',data.emergency?.latched||trade?.emergency?'LATCHED · no automatic rearm':state.emergencyRequested?'Request unresolved · entries locally blocked':'Not latched');
    if(data.account_blocked||data.emergency?.latched)text('entry-status','New acquisition is fenced. Reconciliation and independent protection continue.');
    else if(state.emergencyRequested)text('entry-status','Emergency delivery is unresolved. New entries are blocked locally; restore access and retry EXIT ALL.');
    else if(state.envelope)text('entry-status',state.envelope.invalid?'Local recovery required. New entry stays blocked.':`A ${state.envelope.direction} request is recorded locally. Only a same-command retry is permitted; no request is replayed on refresh.`);
    else if(!brokerReady())text('entry-status','Trading unavailable: verified broker evidence and fresh reconciliation are required. Independent protection stays separate.');
    else if(data.pending_commands?.length)text('entry-status','Managed exposure or acquisition is active. Allocation remains fixed; no discretionary additions.');
    else text('entry-status',state.session?.trading_authorization?.active?'Ready for an authorized offline CALL or PUT. No actual broker orders are enabled.':'Establish separate authorization in Security. IBKR execution remains unavailable.');
    renderAllocation();renderSecurity();setButtons();
  }
  function renderAllocation() {
    const body=$('allocation-rows');body.replaceChildren();const trade=currentTrade();const owned=new Map((trade?.allocation||[]).map(row=>[String(row.strike),row.owned]));
    const lines=state.allocation?.lines||[];
    for(let index=0;index<5;index++){
      const line=lines.find(row=>row.rank===index);const row=document.createElement('tr');
      const quantity=line?.quantity;const actual=line?owned.get(String(line.contract.strike)):null;
      row.append(cell(index===0?'ATM':'OTM+'+index),cell(line?.contract.strike),cell(quantity),cell(actual??(trade?.reconciled_flat?0:null)),
        cell(!line?'Awaiting validated selection':quantity===0?'Not allocated':trade?.reconciled_flat?'Reconciled flat':label(trade?.state||'ACQUIRING')));body.append(row);
    }
    text('planned-total',state.allocation?.total_contracts);text('allocation-note',lines.length?'Fixed, automatic allocation · no manual strike or quantity controls':'Strikes appear only after validated selection.');
  }
  async function recoverCommand() {
    const generation=state.generation,scope=state.scope;
    const pending=state.status?.pending_commands||[];
    const identity=state.envelope?.command_id||pending[pending.length-1]?.command_id;
    if(!identity||state.envelope?.invalid)return;
    try {
      const result=await request('/api/commands/'+encodeURIComponent(identity));
      if(generation!==state.generation||scope!==state.scope)return;
      if(state.envelope&&state.envelope.command_id!==identity)return;
      state.allocation=result.allocation||null;
      if(state.envelope)saveEnvelope({...state.envelope,trade_id:result.trade_id,phase:'RECORDED'});
      const trade=(state.status?.lifecycle?.trades||[]).find(t=>t.trade_id===result.trade_id);
      if(trade?.reconciled_flat&&trade.reservation_state==='RELEASED'){
        const history=await request('/api/history/'+result.trade_id);
        if(generation!==state.generation||scope!==state.scope)return;
        if(state.envelope&&state.envelope.command_id!==identity)return;
        if(['FINALIZED','CANCELLED_NO_FILL'].includes(history.status)){clearEnvelope();if(history.status==='FINALIZED')showFinalized(history);}
      }
    }catch(error){if(generation===state.generation&&error.status!==404)throw error;/* Absence is not proof an in-flight delivery never arrives. */}
  }
  async function refresh(recover=false) {
    if(!state.authenticated||state.refreshing)return;
    state.refreshing=true;const generation=state.generation;
    try {const data=await request('/api/status');if(generation!==state.generation)return;
      state.status=data;if(recover)await recoverCommand();if(generation!==state.generation)return;renderStatus();
    }catch(error){if(generation===state.generation){state.status=null;setButtons();
      text('protection-status','Status unavailable · exposure unresolved');text('supervisor-status','Independent status unverified');
      handleError(error);}}finally{if(generation===state.generation)state.refreshing=false;}
  }
  async function trade(direction) {
    if(!entryEligible(direction))return;state.busy=true;setButtons();const generation=state.generation;
    $('finalized-result').hidden=true;
    try {await securityLock(async()=>{
      if(generation!==state.generation||!await resume())return;
      const status=await request('/api/status');if(generation!==state.generation)return;state.status=status;
      if(state.envelope?.invalid)throw Error('Invalid local metadata');
      if(state.envelope&&state.envelope.direction!==direction)throw Error('Unresolved different command');
      const envelope=state.envelope||{command_id:crypto.randomUUID(),direction,entry_epoch:status.entry_epoch,phase:'PREPARED'};
      saveEnvelope(envelope);text('entry-status','Submitting one durable offline command. A response does not imply fills.');
      const result=await request('/api/commands',{method:'POST',body:{command_id:envelope.command_id,direction:envelope.direction,entry_epoch:envelope.entry_epoch}});
      if(generation!==state.generation)return;
      saveEnvelope({...envelope,trade_id:result.trade_id,phase:'RECORDED'});state.allocation=result.allocation||null;
      if(!state.emergencyBusy&&!state.emergencyRequested)notify('Offline request recorded. Quantities and completion depend on confirmed simulated evidence.');
    });await refresh(true);}
    catch(error){if(generation===state.generation){if(state.envelope&&!state.envelope.invalid)saveEnvelope({...state.envelope,phase:'UNCERTAIN'});handleError(error);}}
    finally {state.busy=false;setButtons();}
  }
  async function exitAll() {
    if(!state.authenticated||state.emergencyBusy)return;
    state.emergencyBusy=true;state.emergencyRequested=true;const generation=state.generation;setButtons();
    let identity=crypto.randomUUID();
    try{const old=localStorage.getItem('mortificatio-emergency:'+state.scope);if(old&&UUID.test(old))identity=old;localStorage.setItem('mortificatio-emergency:'+state.scope,identity);}catch{}
    try {
      // Never queue emergency behind an entry/network lock. Same UUID on a
      // bounded CSRF recovery retry; entries are never retried automatically.
      for(let attempt=0;attempt<2;attempt++){
        if(generation!==state.generation)return;
        if((!state.csrf||attempt>0)&&!await resume())return;
        try {await request('/api/exit-all',{method:'POST',body:{request_id:identity}});break;}
        catch(error){if(attempt===0&&error.code?.includes('CSRF'))continue;throw error;}
      }
      if(generation!==state.generation)return;notify('Emergency request recorded. Acquisition is fenced; liquidation remains pending until broker evidence confirms flat.');await refresh(true);
    }catch(error){handleError(error);}
    finally {state.emergencyBusy=false;setButtons();}
  }
  function showFinalized(record) {
    if(record.status!=='FINALIZED'||record.data_origin!=='OFFLINE_FIXTURE'||!record.totals)return;
    $('finalized-result').hidden=false;text('result-net',money(record.totals.net_realized_pnl));
    $('result-history-link').onclick=event=>{event.preventDefault();navigate('history');loadDetail(record.trade_id);};
  }
  function filterParameters() {
    const params={source:'OFFLINE_FIXTURE',state:$('history-state').value,page:String(state.page),page_size:String(state.pageSize)};
    for(const [id,key] of [['history-search','search'],['history-direction','direction'],['history-start','start_date'],['history-end','end_date']])if($(id).value)params[key]=$(id).value;
    return params;
  }
  async function loadHistory() {
    if(!state.authenticated)return;const generation=state.generation;const requestId=++state.historyGeneration;
    const params=filterParameters();const summary={...params};delete summary.page;delete summary.page_size;delete summary.state;
    try {const [records,stats]=await Promise.all([request('/api/history?'+new URLSearchParams(params)),request('/api/history/summary?'+new URLSearchParams(summary))]);
      if(generation!==state.generation||requestId!==state.historyGeneration)return;
      if(records.data_origin!=='OFFLINE_FIXTURE'||stats.data_origin!=='OFFLINE_FIXTURE')throw Error('Unsupported source');
      state.history=records;text('summary-count',stats.total_completed_trades);text('summary-win',stats.win_rate_percent===null?'—':decimal(stats.win_rate_percent)+'%');
      text('summary-total',money(stats.total_net_realized_pnl));text('summary-average',money(stats.average_net_realized_pnl));
      text('summary-wins',`${stats.wins} wins · ${stats.losses} losses · ${stats.breakevens} breakevens`);renderHistory(records);
    }catch(error){if(generation===state.generation&&requestId===state.historyGeneration){
      $('history-rows').replaceChildren();for(const id of ['summary-count','summary-win','summary-total','summary-average'])text(id,'—');
      text('history-page-info','History verification unavailable; figures withheld.');handleError(error);}}
  }
  function renderHistory(data) {
    const body=$('history-rows');body.replaceChildren();
    for(const record of data.items){const row=document.createElement('tr');const final=record.status==='FINALIZED';
      if(final)state.finalizedIds.add(record.trade_id);
      row.append(cell(dateTime(record.entry_time||record.admission_time)),cell(record.direction),cell(record.total_contracts),cell(record.strike_count),cell(final?money(record.totals?.net_realized_pnl):'Withheld'),
        cell(final?duration(record.duration_seconds):'—'),cell(final?label(record.exit_reason):'Withheld'),cell(label(record.status)));
      const button=document.createElement('button');button.type='button';button.className='quiet row-link';button.textContent='View';button.setAttribute('aria-label','View '+record.direction+' trade '+record.trade_id);
      button.onclick=()=>loadDetail(record.trade_id);const td=cell('');td.append(button);row.append(td);body.append(row);
    }
    if(!data.items.length){const row=document.createElement('tr');const td=cell('No matching records. No performance data has been invented.');td.colSpan=9;row.append(td);body.append(row);}
    text('history-page-info',`Page ${data.page} · ${data.total_records} matching records · synthetic data`);
    $('previous-page').disabled=data.page<=1;$('next-page').disabled=data.page*data.page_size>=data.total_records;
  }
  function fact(term,value) {const box=document.createElement('div');const dt=document.createElement('dt');dt.textContent=term;const dd=document.createElement('dd');dd.textContent=value??'—';box.append(dt,dd);return box;}
  async function loadDetail(identity,{focus=true}={}) {
    const generation=state.generation;state.selectedTrade=identity;
    try {const record=await request('/api/history/'+encodeURIComponent(identity));
      if(generation!==state.generation||identity!==state.selectedTrade)return;
      if(record.data_origin!=='OFFLINE_FIXTURE')throw Error('Unsupported source');
      if(!Number.isSafeInteger(record.revision)||record.revision<(state.detailRevisions.get(identity)||0))return;
      state.detailRevisions.set(identity,record.revision);
      if(record.status==='FINALIZED')state.finalizedIds.add(identity);else state.finalizedIds.delete(identity);
      $('trade-details').hidden=false;text('details-title',record.direction+' · '+record.date);text('details-state',label(record.status)+' · synthetic fixture evidence');
      const facts=$('detail-facts');facts.replaceChildren();const body=$('strike-detail-rows');body.replaceChildren();const final=record.status==='FINALIZED';
      if(final){for(const [term,value] of [['Entry',dateTime(record.entry_time)],['Exit',dateTime(record.exit_time)],['Gross',money(record.totals.gross_realized_pnl)],
        ['Fees',money(record.totals.commissions_and_fees)],['Net realized',money(record.totals.net_realized_pnl)],['Duration',duration(record.duration_seconds)],
        ['Contracts',String(record.total_contracts)],['Exit reason',label(record.exit_reason)]])facts.append(fact(term,value));
        for(const line of record.per_strike){const row=document.createElement('tr');row.append(cell(line.strike),cell(line.entry_quantity),cell(decimal(line.weighted_average_entry)),
          cell(decimal(line.weighted_average_exit)),cell(money(line.gross_realized_pnl)),cell(money(line.commissions_and_fees)),cell(money(line.net_realized_pnl)));body.append(row);}
      }else {facts.append(fact('Result','PENDING RECONCILIATION'),fact('Execution',label(record.status)),fact('Contracts',String(record.total_contracts??'Unknown')));
        text('details-state',label(record.status)+' · results, fill prices and chart evidence remain withheld.');}
      $('strike-detail-container').hidden=!final;$('execution-evidence').hidden=!final;renderChart(final?record.chart:{status:'WITHHELD_BLIND_EXECUTION'});
      const evidence=$('execution-records');evidence.replaceChildren();
      if(final){for(const execution of record.execution_history||[]){const p=document.createElement('p');p.textContent=`${execution.side} · ${execution.quantity} contracts · ${execution.contract.strike} · premium ${decimal(execution.price)} · ${dateTime(execution.source_time)} · ${execution.active?'active revision':'superseded revision'} · ${execution.exec_id}`;evidence.append(p);}
        for(const order of record.order_history||[]){const p=document.createElement('p');p.textContent=`Order ${order.order_id} · ${order.side} · ${order.status} · filled ${order.filled}/${order.quantity}`;evidence.append(p);}}
      if(focus){$('trade-details').focus({preventScroll:true});$('trade-details').scrollIntoView({block:'start',behavior:'auto'});}
    }catch(error){if(generation===state.generation&&identity===state.selectedTrade){
      $('detail-facts').replaceChildren();$('strike-detail-rows').replaceChildren();$('execution-records').replaceChildren();
      renderChart({status:'WITHHELD_BLIND_EXECUTION'});text('details-state','PENDING RECONCILIATION · current verification unavailable.');handleError(error);}}
  }
  function svgElement(name,attributes={}) {const node=document.createElementNS('http://www.w3.org/2000/svg',name);for(const [key,value] of Object.entries(attributes))node.setAttribute(key,String(value));return node;}
  function renderChart(chart) {
    const host=$('chart');host.replaceChildren();
    if(chart.status!=='AVAILABLE') {const p=document.createElement('p');p.className='muted';p.textContent=chart.status==='WITHHELD_BLIND_EXECUTION'?'Chart evidence is withheld until verified finalization.':
      chart.reason==='AMBIGUOUS_SOURCE_TIMESTAMP'?'Chart unavailable: source timestamps cannot establish an unambiguous price history.':'Chart unavailable: insufficient recorded SPX history. No prices or candles are inferred.';host.append(p);text('chart-note','Only recorded source evidence may be displayed.');return;}
    const points=chart.observations||[];const markers=chart.execution_markers||[];
    const dates=[...points.map(p=>Date.parse(p.source_time)),...markers.map(p=>Date.parse(p.source_time))];const prices=points.map(p=>Number(p.price));
    if(points.length<2||dates.some(x=>!Number.isFinite(x))||prices.some(x=>!Number.isFinite(x))){renderChart({status:'UNAVAILABLE'});return;}
    const minT=Math.min(...dates),maxT=Math.max(...dates),minP=Math.min(...prices),maxP=Math.max(...prices);
    const x=t=>48+(Date.parse(t)-minT)/Math.max(1,maxT-minT)*650;const y=p=>195-(Number(p)-minP)/Math.max(.01,maxP-minP)*155;
    const svg=svgElement('svg',{viewBox:'0 0 750 260',role:'img','aria-label':'Recorded synthetic SPX observations and source-timestamped entry and exit markers. No candles or interpolated price path.'});
    svg.append(svgElement('line',{x1:48,y1:210,x2:708,y2:210,class:'axis'}));
    for(const p of points){const node=svgElement('circle',{cx:x(p.source_time),cy:y(p.price),r:3,class:'price-point'});const title=svgElement('title');title.textContent=`SPX ${p.price} · ${p.source_time} · ${p.source}`;node.append(title);svg.append(node);}
    for(const marker of markers){const cy=marker.spx_price===null?232:y(marker.spx_price);const node=svgElement('circle',{cx:x(marker.source_time),cy,r:4.5,class:marker.side==='BUY'?'entry-marker':'exit-marker'});const title=svgElement('title');title.textContent=`${marker.side} ${marker.quantity} · ${marker.source_time} · ${marker.spx_price===null?'time only; SPX price unavailable':'recorded SPX '+marker.spx_price}`;node.append(title);svg.append(node);}
    for(const [px,py,value] of [[5,45,String(maxP)],[5,198,String(minP)],[48,255,dateTime(new Date(minT).toISOString())],[490,255,dateTime(new Date(maxT).toISOString())]]){const node=svgElement('text',{x:px,y:py});node.textContent=value;svg.append(node);}
    host.append(svg);text('chart-note','Gold: recorded SPX observations. Green/red: entry/exit. Bottom markers are time-only when no exact SPX price exists. No interpolation or candles.');
    const table=document.createElement('table');table.className='sr-only';const caption=document.createElement('caption');caption.textContent='Recorded chart evidence';table.append(caption);
    for(const p of points){const row=document.createElement('tr');row.append(cell(p.source_time),cell(p.price),cell(p.source));table.append(row);}host.append(table);
  }
  function navigate(page,update=true) {
    state.pageName=pageNames.has(page)?page:'dashboard';
    for(const node of document.querySelectorAll('[data-screen]'))node.hidden=node.dataset.screen!==state.pageName;
    for(const node of document.querySelectorAll('[data-page]')){if(node.dataset.page===state.pageName)node.setAttribute('aria-current','page');else node.removeAttribute('aria-current');}
    if(update)history.pushState({},'',state.pageName==='dashboard'?'/dashboard':'/'+state.pageName);
    if(state.authenticated&&state.pageName==='history')loadHistory();
    $('account-recovery').hidden=state.authenticated;
    $('key-management').hidden=!state.authenticated||state.pageName!=='security';
    if(state.authenticated&&state.pageName==='security')loadKeys();
    if(update)$('main').focus({preventScroll:true});
  }
  function initialize() {
    $('login').onclick=login;$('enroll').onclick=enroll;$('logout').onclick=logout;$('authorize').onclick=authorize;$('revoke-sessions').onclick=revokeSessions;
    $('call').onclick=()=>trade('CALL');$('put').onclick=()=>trade('PUT');$('exit-all').onclick=exitAll;
    $('recover-key').onclick=recoverKey;$('add-login-key').onclick=()=>keyChange('ADD_LOGIN');
    $('add-recovery-key').onclick=()=>keyChange('ADD_RECOVERY');$('revoke-key').onclick=()=>keyChange('REVOKE');
    $('credential-target').onchange=()=>{$('revoke-key').disabled=!$('credential-target').value;};
    for(const node of document.querySelectorAll('[data-page]'))node.onclick=event=>{event.preventDefault();navigate(node.dataset.page);};
    $('history-filters').onsubmit=event=>{event.preventDefault();state.page=1;loadHistory();};
    $('history-page-size').onchange=()=>{state.pageSize=Number($('history-page-size').value);state.page=1;loadHistory();};
    $('previous-page').onclick=()=>{if(state.page>1){state.page--;loadHistory();}};
    $('next-page').onclick=()=>{state.page++;loadHistory();};$('close-details').onclick=()=>{$('trade-details').hidden=true;state.selectedTrade=null;$('history-search').focus();};
    window.addEventListener('popstate',()=>navigate(location.pathname.split('/')[1],false));
    window.addEventListener('storage',event=>{if(state.scope&&event.key==='mortificatio-command:'+state.scope){state.envelope=readEnvelope();setButtons();}
      if(state.scope&&event.key==='mortificatio-emergency:'+state.scope){state.emergencyRequested=true;setButtons();}});
    navigate(location.pathname.split('/')[1],false);renderAllocation();
    securityLock(async()=>{try{await resume();await refresh(true);if(state.pageName==='history')await loadHistory();}catch(error){if(error.status===401)authLost('Sign in to unlock private trading and history information.');else handleError(error);}});
    setInterval(async()=>{if(state.authenticated){renderSecurity();setButtons();const recover=++state.pollCount%4===0;await refresh(recover);
      if(recover&&state.authenticated&&state.pageName==='history'){await loadHistory();if(state.selectedTrade)await loadDetail(state.selectedTrade,{focus:false});}}},2500);
  }
  initialize();
}());
