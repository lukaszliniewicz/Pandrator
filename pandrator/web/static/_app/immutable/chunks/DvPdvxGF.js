import"./DsnmJJEf.js";import{g as $,f as A,a as i,s as p,d,i as R,r as l,c as k,t as g,e as h}from"./DoBfy4gY.js";import{i as c}from"./7c39q4te.js";import{p as u}from"./BlltrVBt.js";var b=k('<p class="mt-1"> </p>'),C=k(`<div class="rounded-xl border border-amber-500/40 p-3 text-sm" role="status"><p class="font-semibold">Some recordings use earlier or unverified settings.</p> <!> <!> <p class="mt-1">“Continue unfinished audio” deliberately keeps these recordings. Use
      “Refresh changed audio…” to regenerate them with the current voice and
      settings. Existing takes remain available for playback and selection.</p></div>`);function z(_,f){let a=u(f,"settingsStale",3,0),r=u(f,"identityUnknown",3,0);var v=$(),x=A(v);{var w=s=>{var o=C(),m=p(d(o),2);{var y=e=>{var t=b(),n=d(t);l(t),g(()=>h(n,`${a()??""}
        ${a()===1?"block has":"blocks have"} audio from different generation
        settings or an older voice reference.`)),i(e,t)};c(m,e=>{a()&&e(y)})}var S=p(m,2);{var U=e=>{var t=b(),n=d(t);l(t),g(()=>h(n,`${r()??""}
        ${r()===1?"older take was":"older takes were"} made with
        earlier settings, or ${r()===1?"its":"their"} saved settings
        could not be verified.`)),i(e,t)};c(S,e=>{r()&&e(U)})}R(2),l(o),i(s,o)};c(x,s=>{(a()||r())&&s(w)})}i(_,v)}export{z as A};
