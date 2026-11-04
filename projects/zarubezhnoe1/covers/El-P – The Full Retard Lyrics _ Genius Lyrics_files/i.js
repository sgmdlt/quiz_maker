(function() { function bxBootstrap() {
	var re = /bot|crawl|slurp|spider|mediapartners|headlesschrome|snap-prefetch|remotasks|woorank|uptime\.com|facebookexternalhit|facebookcatalog/i;
	if (re.test(navigator.userAgent) || navigator.userAgent == '') {
		return;
	}

	if (!(window.bouncex&&bouncex.website)) {
		var pushedData = [];
		var pushedClientEvents = [];
		if(window.bouncex && bouncex.push && bouncex.length){
			pushedData = bouncex;
		}
		if (window.bouncex && bouncex.clientEvents && bouncex.clientEvents.length) {
           pushedClientEvents = bouncex.clientEvents;
        }
		window.bouncex = {};
		bouncex.pushedData = pushedData;
		bouncex.pushedClientEvents = pushedClientEvents;
		bouncex.website = {"id":5453,"name":"MediaLab | Genius","cookie_name":"bounceClientVisit5453","domain":"genius.com","ct":"fp_local_storage","ally":0,"ei":0,"tcjs":"","cjs":"/* enable.feature.TCF_ENABLE_NEW_PURPOSE_LIST */","force_https":false,"waypoints":false,"content_width":900,"gai":"","swids":"","sd":0,"ljq":"auto","campaign_id":0,"is_preview":false,"aco":{"first_party_limit":"3500","local_storage":"1"},"cmp":{"gdpr":1,"gmp":0,"whitelist_check":0},"burls":[],"ple":false,"fbe":true,"ffs":"INTENT_IQ_INTEGRATION","mas":2,"map":1,"gar":true,"ete":2,"ettm":true,"etjs":"","dge":false,"bxidLoadFirst":false,"pie":false,"cme":false,"gbi_enabled":1,"bpush":false,"pt":null,"els":{"masthead":"div[class*=\"Leaderboard__LeaderboardOrMarquee\"], #sticky-nav"},"vars":[{"name":"dfp_rblock","polling":"all","persist":"no","page_types":[],"testmode":false,"default":"false","code":"(function() {\n    if (!bouncex.dfpRan) {\n        bouncex.dfpRan = true;\n        return 'not_ready';\n    }\n    function check() {\n        bouncex.dfp_loaded = false || bouncex.dfp_loaded;\n\n        var event = bouncex.gbi2.getDfpEndPageViewEvent();\n\n        if (event && event.dfp_status) {\n            if (event.dfp_status === 'pending') {\n                return 'not_ready';\n            }\n\n            bouncex.dfp_loaded = true;\n\n            if (event.dfp_status === 'suppressed') {\n                return 'sponsor';\n            }\n            return 'allow';\n        }\n\n        return 'not_ready';\n    }\n\n    if (bouncex.website.gbi.rblocks) {\n        if (bouncex.dfp_loaded) {\n            return null;\n        } else if (window.googletag && window.googletag.apiReady && window.googletag.pubadsReady && bouncex.gbi2) {\n            return check();\n        } else if (typeof window.googletag === 'undefined') {\n            return 'no_googletag';\n        }\n        return 'fallback';\n    } else {\n        return 'not_ready';\n    }\n})();","trigger":"pageload"},{"name":"video_present","polling":"none","persist":"no","page_types":[],"testmode":false,"default":"false","code":"document.querySelectorAll('.video_page').length > 0;","trigger":"pageload"}],"dgu":"pixel.cdnwidget.com","dgp":false,"ba":{"enabled":1,"fbte":0},"biu":"assets.bounceexchange.com","bau":"api.bounceexchange.com","beu":"events.bouncex.net","ibx":{"tjs":"","cjs":"","miw":0,"mibcx":1,"te":0,"cart_rep":{"get":"","set":""},"ulpj":null,"cus":"","miw_exclude":"","enabled":0},"etjson":null,"osre":true,"osru":"osr.bounceexchange.com/v1/osr/items","checkDfp":true,"gamNetwork":"342026871","spa":0,"spatm":1,"preinit_cjs":"","mat":0,"math":0,"cpnu":"coupons.bounceexchange.com","dfpcms":0,"sms":{"optm":"","eventSharing":false,"shqId":"","enabled":0},"pde":true,"fmc":["AX","AL","AD","AM","AT","AZ","BY","BE","BA","BG","HR","CY","CZ","DK","EE","EU","FO","FI","FR","GI","GG","GE","DE","GR","HU","IM","IS","IE","IT","JE","LV","LI","LT","LU","MT","MD","MC","ME","NL","MK","NO","PL","PT","RO","SM","RS","SK","SI","ES","SJ","SE","CH","UA","GB","VA","US","CA"],"fme":true,"fmx":"","uid2":true,"iiq":true,"sdk":{"android":{"enabled":false,"enabledVersions":[],"eventModifications":null},"ios":{"enabled":false,"enabledVersions":[],"eventModifications":null}},"onsite":{"enabled":1},"ads":{"enabled":1},"pubs":{"enabled":1},"websdk":{"enabled":0,"devMode":1},"ga4_property_id":"","ga4_measurement_id":"","tag_state_domain":"","tag_state_domain_enabled":false,"tag_state_sst_enabled":false}
;

		bouncex.tag = 'tag3';
		bouncex.$ = window.jQuery;
		bouncex.env = 'production';
		bouncex.restrictedTlds = {"casl":{"ca":1},"gdpr":{"ad":1,"al":1,"at":1,"ax":1,"ba":1,"be":1,"bg":1,"by":1,"xn--90ais":1,"ch":1,"cy":1,"cz":1,"de":1,"dk":1,"ee":1,"es":1,"eu":1,"fi":1,"fo":1,"fr":1,"uk":1,"gb":1,"gg":1,"gi":1,"gr":1,"hr":1,"hu":1,"ie":1,"im":1,"is":1,"it":1,"je":1,"li":1,"lt":1,"lu":1,"lv":1,"mc":1,"md":1,"me":1,"mk":1,"xn--d1al":1,"mt":1,"nl":1,"no":1,"pl":1,"pt":1,"ro":1,"rs":1,"xn--90a3ac":1,"ru":1,"su":1,"xn--p1ai":1,"se":1,"si":1,"sj":1,"sk":1,"sm":1,"ua":1,"xn--j1amh":1,"va":1,"tr":1}};
		bouncex.client = {
			supportsBrotli: 1
		};
		bouncex.assets = {"ads":"f6d1862dcbb965ef69de713c0336381a","creativesBaseStyles":"8f3f9b9c","gam_342026871":"//assets.bounceexchange.com/assets/gam/342026871/62c07ad8fe14c46207b4d9238ef0f47e.json","gpsAuction":"bbb80866120d17013073bb6d284cbd6b","inbox":"f73dad2942eb5f670a0dcf2df7b37067","onsite":"1238d00d70e4de87443df9eb69ff1a61","sms":"e39203556bab2366e56296ce42e974a7","websdk":"69febcf72b8657b48fa4dc9ba5d3dfca","website_campaigns_5453":"8e3e2e52d42e31b74c66a07e97e68592"};
		bouncex.push = function(pushData) {
			bouncex.pushedData.push(pushData);
		}

		var runtime = document.createElement('script');
		runtime.setAttribute('src', '//assets.bounceexchange.com/assets/smart-tag/versioned/runtime_c81e76ee00d795b1eebf8d27949f8dc5.br.js');
		runtime.setAttribute('async', 'async');

		bouncex.initializeTag = function() {
			var script = document.createElement('script');
			script.setAttribute('src', '//assets.bounceexchange.com/assets/smart-tag/versioned/main-v2_3d4b691a9746c7f5eb6366a7e352ffad.br.js');
			script.setAttribute('async', 'async');
			document.body.appendChild(script);


			bouncex.initializeTag = function() {};
		};

		runtime.onload = bouncex.initializeTag;
		document.body.appendChild(runtime);

	}


}

if (document.readyState === "loading") {
	document.addEventListener("DOMContentLoaded", bxBootstrap);
} else {
	bxBootstrap();
}})();