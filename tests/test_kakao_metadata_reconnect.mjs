import assert from 'node:assert/strict';
import test from 'node:test';
import {lookupAndVerifyChatsByIds} from '../bridge/kakao_direct_bridge.mjs';

const room = id => ({chat_id:id,type:'DirectChat',active_members:2,title:`상대${id}`});
const members = id => ({chat_id:id,complete:true,active_members:2,
  members:[{user_id:'1000',nickname:'나'},{user_id:String(2000+Number(id)),nickname:`상대${id}`}]});
const disconnected = () => Object.assign(new Error('PRIVATE'), {code:'get_chat_failed',serverStatus:-1});

test('first six disconnected rooms are retried once after the other 25 finish', async () => {
  const ids=Array.from({length:31},(_,i)=>String(i+1));
  const calls=[], counts=new Map();
  const result=await lookupAndVerifyChatsByIds(ids,{ownUserId:'1000',
    getChat:async id=>{
      calls.push(id);counts.set(id,(counts.get(id)??0)+1);
      if(Number(id)<=6 && counts.get(id)===1)throw disconnected();
      return room(id);
    },getMemberSnapshot:async id=>members(id)});
  assert.equal(result.partial_failure,false);
  assert.deepEqual(result.chats.map(r=>r.chat_id),ids);
  assert.deepEqual(calls.slice(31),ids.slice(0,6));
  assert.equal(result.chats.every(r=>r.peer_user_id),true);
});

test('a disconnected member snapshot causes a complete room and member reread', async () => {
  let reads=0,snapshots=0;
  const result=await lookupAndVerifyChatsByIds(['1'],{ownUserId:'1000',
    getChat:async id=>{reads++;return room(id);},
    getMemberSnapshot:async id=>{if(++snapshots===1)throw disconnected();return members(id);}});
  assert.equal(reads,2);assert.equal(snapshots,2);assert.equal(result.partial_failure,false);
});

test('persistent disconnection stops after one retry and never invents a match', async () => {
  let reads=0;
  const result=await lookupAndVerifyChatsByIds(['1'],{ownUserId:'1000',
    getChat:async()=>{reads++;throw disconnected();},getMemberSnapshot:async()=>{throw new Error('unexpected');}});
  assert.equal(reads,2);assert.deepEqual(result,{chats:[],partial_failure:true});
});

test('server rejection, authentication and malformed rows are not retried', async () => {
  for(const error of [Object.assign(new Error(),{code:'get_chat_failed',serverStatus:-401}),
    Object.assign(new Error(),{code:'invalid_access_token'}),new Error('PRIVATE')]){
    let reads=0;
    const result=await lookupAndVerifyChatsByIds(['1'],{ownUserId:'1000',
      getChat:async()=>{reads++;throw error;},getMemberSnapshot:async id=>members(id)});
    assert.equal(reads,1);assert.equal(result.partial_failure,true);
  }
  let reads=0;
  const result=await lookupAndVerifyChatsByIds(['1'],{ownUserId:'1000',
    getChat:async()=>{reads++;return room('2');},getMemberSnapshot:async id=>members(id)});
  assert.equal(reads,1);assert.equal(result.partial_failure,true);
});

test('a failed member verification after recovery still blocks the room', async()=>{
  let reads=0;
  const result=await lookupAndVerifyChatsByIds(['1'],{ownUserId:'1000',
    getChat:async id=>{if(++reads===1)throw disconnected();return room(id);},
    getMemberSnapshot:async id=>({...members(id),complete:false})});
  assert.equal(result.partial_failure,true);
  assert.equal(result.chats[0].peer_user_id,null);
});
