import"./DsnmJJEf.js";import{h as $,f as A,a as s,s as p,d,i as R,r as l,c as k,t as g,e as h}from"./RHWhEHlT.js";import{p as u,i as c}from"./CS2_Oa3S.js";var b=k('<p class="mt-1"> </p>'),C=k(`<div class="rounded-xl border border-amber-500/40 p-3 text-sm" role="status"><p class="font-semibold">Some recordings use earlier or unverified settings.</p> <!> <!> <p class="mt-1">“Continue unfinished audio” deliberately keeps these recordings. Use
      “Refresh changed audio…” to regenerate them with the current voice and
      settings. Existing takes remain available for playback and selection.</p></div>`);function q(_,v){let r=u(v,"settingsStale",3,0),a=u(v,"identityUnknown",3,0);var f=$(),x=A(f);{var w=i=>{var o=C(),m=p(d(o),2);{var y=e=>{var t=b(),n=d(t);l(t),g(()=>h(n,`${r()??""}
        ${r()===1?"block has":"blocks have"} audio from different generation
        settings or an older voice reference.`)),s(e,t)};c(m,e=>{r()&&e(y)})}var S=p(m,2);{var U=e=>{var t=b(),n=d(t);l(t),g(()=>h(n,`${a()??""}
        ${a()===1?"older take was":"older takes were"} made with
        earlier settings, or ${a()===1?"its":"their"} saved settings
        could not be verified.`)),s(e,t)};c(S,e=>{a()&&e(U)})}R(2),l(o),s(i,o)};c(x,i=>{(r()||a())&&i(w)})}s(_,f)}export{q as A};
