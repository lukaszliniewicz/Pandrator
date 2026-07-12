<script lang="ts">import SessionWorkspace from '$lib/SessionWorkspace.svelte';import {useSessionContext} from '$lib/session-context';const context=useSessionContext();</script>
{#if context.session}<SessionWorkspace session={context.session} onback={()=>location.href='/sessions'} onupdated={(session)=>{context.session=session;context.reload()}}/>{/if}
