import"./DsnmJJEf.js";import{e as U,f as $,a as i,s as v,g as A,i as R,r as C,c as b,o as m,t as p,d as g}from"./BF7tQQAS.js";import{i as d}from"./CsjbbcMS.js";import{p as h}from"./C6Rn0klq.js";var u=b('<p class="mt-1"> </p>'),E=b(`<div class="rounded-xl border border-amber-500/40 p-3 text-sm" role="status"><p class="font-semibold">Some recordings use earlier or unverified settings.</p> <!> <!> <p class="mt-1">“Continue unfinished audio” deliberately keeps these recordings. Use
      “Refresh changed audio…” to regenerate them with the current voice and
      settings. Existing takes remain available for playback and selection.</p></div>`);function B(_,l){let a=h(l,"settingsStale",3,0),t=h(l,"identityUnknown",3,0);var c=U(),k=$(c);{var x=s=>{var o=E(),f=v(A(o),2);{var w=e=>{var r=u(),n=m(r);p(()=>g(n,`${a()??""}
        ${a()===1?"block has":"blocks have"} audio from different generation
        settings or an older voice reference.`)),i(e,r)};d(f,e=>{a()&&e(w)})}var y=v(f,2);{var S=e=>{var r=u(),n=m(r);p(()=>g(n,`${t()??""}
        ${t()===1?"older take was":"older takes were"} made with
        earlier settings, or ${t()===1?"its":"their"} saved settings
        could not be verified.`)),i(e,r)};d(y,e=>{t()&&e(S)})}R(2),C(o),i(s,o)};d(k,s=>{(a()||t())&&s(x)})}i(_,c)}export{B as A};
