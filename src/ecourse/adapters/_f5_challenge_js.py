"""JavaScript constants for the F5 BIG-IP / Shape Security challenge solver.

These are the browser-environment mock and two-phase runner scripts executed
in Duktape (via dukpy) to solve the obfuscated challenge without a real
browser.
"""

MOCK_BROWSER_JS = r"""
var __captured = { xhr_calls: [], cookies: [], image_srcs: [], errors: [] };

var window = this;
window.window = window;
window.self = window;
window.top = window;
window.parent = window;
window.frames = [];
window.length = 0;
window.name = "";
window.closed = false;
window.opener = null;
window.frameElement = null;
window.AUMq = false;

window.location = {
    href: "https://elearn.nthu.edu.tw/",
    protocol: "https:", host: "elearn.nthu.edu.tw",
    hostname: "elearn.nthu.edu.tw", port: "", pathname: "/",
    search: "", hash: "", origin: "https://elearn.nthu.edu.tw",
    reload: function(){}, replace: function(){}, assign: function(){},
    toString: function(){ return this.href; },
    ancestorOrigins: { length: 0 }
};

window.navigator = {
    userAgent: "@@UA@@",
    appCodeName: "Mozilla", appName: "Netscape",
    appVersion: "5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
    platform: "MacIntel", product: "Gecko", productSub: "20030107",
    vendor: "Google Inc.", vendorSub: "", language: "zh-TW",
    languages: ["zh-TW","zh","en-US","en"], onLine: true,
    cookieEnabled: true, doNotTrack: null, maxTouchPoints: 0,
    hardwareConcurrency: 8, deviceMemory: 8,
    connection: { effectiveType: "4g", rtt: 50, downlink: 10, saveData: false },
    plugins: { length: 5, item: function(){return null}, namedItem: function(){return null}, refresh: function(){} },
    mimeTypes: { length: 2, item: function(){return null}, namedItem: function(){return null} },
    javaEnabled: function(){return false}, getGamepads: function(){return []},
    sendBeacon: function(){return true}, webdriver: false,
    getBattery: function(){return null},
    mediaDevices: { enumerateDevices: function(){return []} },
    permissions: { query: function(){return null} },
    serviceWorker: { controller: null }, storage: {}, credentials: {},
    clipboard: {}, geolocation: {}, locks: {}, scheduling: {},
    userActivation: { hasBeenActive: true, isActive: false },
    ink: {}, hid: {}, serial: {}, bluetooth: {}, usb: {}, xr: {},
    managed: {}, windowControlsOverlay: {}, gpu: {}
};

window.screen = {
    width: 1920, height: 1080, availWidth: 1920, availHeight: 1055,
    colorDepth: 24, pixelDepth: 24, availLeft: 0, availTop: 25,
    orientation: { type: "landscape-primary", angle: 0 }
};
window.screenX = 0; window.screenY = 25; window.screenLeft = 0; window.screenTop = 25;
window.innerWidth = 1920; window.innerHeight = 969;
window.outerWidth = 1920; window.outerHeight = 1055;
window.devicePixelRatio = 1;
window.pageXOffset = 0; window.pageYOffset = 0;
window.scrollX = 0; window.scrollY = 0;

var __startTime = Date.now();
window.performance = {
    now: function(){ return Date.now() - __startTime; },
    timing: {
        navigationStart: __startTime, fetchStart: __startTime+1,
        domainLookupStart: __startTime+2, domainLookupEnd: __startTime+10,
        connectStart: __startTime+10, connectEnd: __startTime+50,
        requestStart: __startTime+50, responseStart: __startTime+100,
        responseEnd: __startTime+200, domLoading: __startTime+200,
        domInteractive: __startTime+500, domContentLoadedEventStart: __startTime+500,
        domContentLoadedEventEnd: __startTime+510, domComplete: __startTime+600,
        loadEventStart: __startTime+600, loadEventEnd: __startTime+610
    },
    navigation: { type: 0, redirectCount: 0 },
    getEntriesByType: function(){return []}, getEntriesByName: function(){return []},
    getEntries: function(){return []}, mark: function(){}, measure: function(){},
    clearMarks: function(){}, clearMeasures: function(){}
};

var __eventHandlers = {};
window.addEventListener = function(type, fn) {
    if (!__eventHandlers[type]) __eventHandlers[type] = [];
    __eventHandlers[type].push(fn);
};
window.removeEventListener = function(type, fn) {
    if (!__eventHandlers[type]) return;
    for (var i = 0; i < __eventHandlers[type].length; i++) {
        if (__eventHandlers[type][i] === fn) { __eventHandlers[type].splice(i, 1); break; }
    }
};
window.attachEvent = function(type, fn) { window.addEventListener(type.replace(/^on/, ""), fn); };
window.detachEvent = function(){};
window.postMessage = function(){};
window.dispatchEvent = function(evt) {
    var type = evt && evt.type;
    if (type && __eventHandlers[type]) {
        for (var i = 0; i < __eventHandlers[type].length; i++) {
            try { __eventHandlers[type][i](evt); } catch(e) { __captured.errors.push("event:" + e.toString()); }
        }
    }
    return true;
};

var __docEventHandlers = {};
var document = {
    cookie: "", _cookie: "", title: "", domain: "elearn.nthu.edu.tw",
    referrer: "", URL: "https://elearn.nthu.edu.tw/",
    documentURI: "https://elearn.nthu.edu.tw/",
    compatMode: "CSS1Compat", characterSet: "UTF-8", charset: "UTF-8",
    contentType: "text/html", readyState: "complete",
    visibilityState: "visible", hidden: false,
    hasFocus: function(){ return true; },
    documentElement: {
        tagName: "HTML", style: {},
        getAttribute: function(){return null}, setAttribute: function(){},
        classList: { add:function(){}, remove:function(){}, contains:function(){return false} },
        scrollTop: 0, scrollLeft: 0, clientWidth: 1920, clientHeight: 969,
        offsetWidth: 1920, offsetHeight: 969
    },
    head: {
        tagName: "HEAD", innerHTML: "", childNodes: [],
        appendChild: function(c){return c}, insertBefore: function(a){return a}
    },
    body: {
        tagName: "BODY", innerHTML: "", childNodes: [],
        appendChild: function(c){return c}, insertBefore: function(a){return a},
        style: {}, offsetWidth: 1920, offsetHeight: 0,
        scrollWidth: 1920, scrollHeight: 0, clientWidth: 1920, clientHeight: 0,
        getAttribute: function(){return null}, setAttribute: function(){},
        addEventListener: function(t,fn){ document.addEventListener(t,fn); },
        removeEventListener: function(){}
    },
    getElementsByTagName: function(tag) {
        if (tag==="head") return [this.head];
        if (tag==="body") return [this.body];
        if (tag==="html") return [this.documentElement];
        return [];
    },
    getElementsByClassName: function(){return []},
    getElementsByName: function(){return []},
    getElementById: function(){return null},
    querySelector: function(){return null},
    querySelectorAll: function(){return []},
    createElement: function(tag) {
        var el = {
            tagName: tag.toUpperCase(), style: {}, innerHTML: "", innerText: "",
            textContent: "", childNodes: [], children: [], parentNode: null,
            className: "", id: "", src: "", href: "", type: "",
            width: 0, height: 0,
            getAttribute: function(){return null},
            setAttribute: function(k,v){ this[k]=v; },
            removeAttribute: function(){},
            appendChild: function(c){ this.childNodes.push(c); return c; },
            removeChild: function(){},
            insertBefore: function(a){return a},
            cloneNode: function(){ return document.createElement(tag); },
            addEventListener: function(){}, removeEventListener: function(){},
            getBoundingClientRect: function(){ return {top:0,left:0,right:0,bottom:0,width:0,height:0}; },
            getContext: function(type) {
                if (type === "2d") {
                    return {
                        fillRect:function(){}, clearRect:function(){}, strokeRect:function(){},
                        beginPath:function(){}, closePath:function(){}, moveTo:function(){},
                        lineTo:function(){}, arc:function(){}, fill:function(){}, stroke:function(){},
                        fillText:function(){}, strokeText:function(){},
                        measureText:function(t){return {width:t.length*8}},
                        setTransform:function(){}, drawImage:function(){},
                        save:function(){}, restore:function(){},
                        createLinearGradient:function(){return {addColorStop:function(){}}},
                        createRadialGradient:function(){return {addColorStop:function(){}}},
                        getImageData:function(x,y,w,h){return {data:new Array(w*h*4)}},
                        putImageData:function(){},
                        canvas:{toDataURL:function(){return "data:image/png;base64,mock"}},
                        globalAlpha:1, globalCompositeOperation:"source-over",
                        font:"10px sans-serif", textAlign:"start", textBaseline:"alphabetic",
                        fillStyle:"#000000", strokeStyle:"#000000", lineWidth:1,
                        lineCap:"butt", lineJoin:"miter", shadowBlur:0,
                        shadowColor:"rgba(0,0,0,0)", shadowOffsetX:0, shadowOffsetY:0
                    };
                }
                return null;
            },
            toDataURL: function(){return "data:image/png;base64,mock"},
            dispatchEvent: function(){return true}, focus:function(){}, blur:function(){},
            click:function(){}, matches:function(){return false},
            closest:function(){return null}, contains:function(){return false}
        };
        return el;
    },
    createTextNode: function(text){ return {nodeType:3,textContent:text,nodeValue:text}; },
    createEvent: function(){ return {initEvent:function(){}}; },
    createDocumentFragment: function(){
        return { appendChild:function(c){return c}, childNodes:[], querySelector:function(){return null}, querySelectorAll:function(){return []} };
    },
    addEventListener: function(type, fn) {
        if (!__docEventHandlers[type]) __docEventHandlers[type] = [];
        __docEventHandlers[type].push(fn);
    },
    removeEventListener: function(){},
    dispatchEvent: function(){return true},
    write:function(){}, writeln:function(){}, open:function(){}, close:function(){},
    execCommand:function(){return false},
    implementation: { hasFeature:function(){return false}, createHTMLDocument:function(){return document} },
    defaultView: null, activeElement: null, lastModified: "", location: null
};
document.defaultView = window;
document.location = window.location;
document.activeElement = document.body;
window.document = document;

try {
    Object.defineProperty(document, "cookie", {
        get: function(){ return document._cookie || ""; },
        set: function(v){ document._cookie = v; __captured.cookies.push(v); }
    });
} catch(e) {}

function XMLHttpRequest() {
    this.readyState=0; this.status=0; this.statusText=""; this.responseText="";
    this.responseXML=null; this.response=""; this.responseType=""; this.timeout=0;
    this.withCredentials=false; this.onreadystatechange=null; this.onload=null;
    this.onerror=null; this.ontimeout=null; this._headers={}; this._method="";
    this._url=""; this._async=true;
}
XMLHttpRequest.prototype.open = function(m,u,a){ this._method=m; this._url=u; this._async=a!==false; this.readyState=1; };
XMLHttpRequest.prototype.setRequestHeader = function(k,v){ this._headers[k]=v; };
XMLHttpRequest.prototype.send = function(body){
    __captured.xhr_calls.push({ method:this._method, url:this._url, body:body||null, headers:this._headers });
    if (this._url.indexOf("type=13") !== -1) {
        __captured.pending_xhr = this;
        return;
    }
    this.readyState=4; this.status=200; this.statusText="OK"; this.responseText="";
    if (typeof this.onreadystatechange==="function") try{this.onreadystatechange()}catch(e){}
    if (typeof this.onload==="function") try{this.onload()}catch(e){}
};
XMLHttpRequest.prototype.abort=function(){};
XMLHttpRequest.prototype.getAllResponseHeaders=function(){return ""};
XMLHttpRequest.prototype.getResponseHeader=function(){return null};
XMLHttpRequest.prototype.overrideMimeType=function(){};
XMLHttpRequest.prototype.addEventListener=function(e,fn){
    if(e==="load")this.onload=fn; if(e==="error")this.onerror=fn;
    if(e==="readystatechange")this.onreadystatechange=fn;
};
XMLHttpRequest.prototype.removeEventListener=function(){};
window.XMLHttpRequest = XMLHttpRequest;
function ActiveXObject(){ return new XMLHttpRequest(); }
window.ActiveXObject = ActiveXObject;

function Image(w,h){
    this.width=w||0; this.height=h||0; this.complete=true;
    this.naturalWidth=0; this.naturalHeight=0; this._src="";
    var self=this;
    Object.defineProperty(this,"src",{
        get:function(){return self._src},
        set:function(v){self._src=v; __captured.image_srcs.push(v);
            if(typeof self.onload==="function") try{self.onload()}catch(e){}}
    });
}
window.Image = Image;

var __timerQueue = [], __timerId = 0;
window.setTimeout = function(fn,delay){
    __timerId++; var id=__timerId;
    if(typeof fn==="function") __timerQueue.push({id:id,fn:fn,delay:delay||0,type:"timeout"});
    return id;
};
window.clearTimeout = function(id){
    for(var i=0;i<__timerQueue.length;i++) if(__timerQueue[i].id===id){__timerQueue.splice(i,1);break;}
};
window.setInterval = function(fn,delay){
    __timerId++; var id=__timerId;
    if(typeof fn==="function") __timerQueue.push({id:id,fn:fn,delay:delay||0,type:"interval"});
    return id;
};
window.clearInterval = window.clearTimeout;
window.requestAnimationFrame = function(fn){ return window.setTimeout(fn,16); };
window.cancelAnimationFrame = window.clearTimeout;

window.atob = function(s){
    var c="ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/=",o="";
    s=s.replace(/[^A-Za-z0-9\+\/\=]/g,"");
    for(var i=0;i<s.length;){
        var e1=c.indexOf(s.charAt(i++)),e2=c.indexOf(s.charAt(i++)),
            e3=c.indexOf(s.charAt(i++)),e4=c.indexOf(s.charAt(i++));
        o+=String.fromCharCode((e1<<2)|(e2>>4));
        if(e3!==64) o+=String.fromCharCode(((e2&15)<<4)|(e3>>2));
        if(e4!==64) o+=String.fromCharCode(((e3&3)<<6)|e4);
    }
    return o;
};
window.btoa = function(s){
    var c="ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/=",o="";
    for(var i=0;i<s.length;){
        var c1=s.charCodeAt(i++),c2=i<s.length?s.charCodeAt(i++):NaN,c3=i<s.length?s.charCodeAt(i++):NaN;
        o+=c.charAt(c1>>2)+c.charAt(((c1&3)<<4)|(c2>>4));
        o+=isNaN(c2)?'=':c.charAt(((c2&15)<<2)|(c3>>6));
        o+=isNaN(c3)?'=':c.charAt(c3&63);
    }
    return o;
};

window.console = {log:function(){},error:function(){},warn:function(){},info:function(){},
    debug:function(){},dir:function(){},trace:function(){},time:function(){},timeEnd:function(){},
    group:function(){},groupEnd:function(){},assert:function(){}};
window.getComputedStyle = function(){ return {getPropertyValue:function(){return ""},length:0}; };
window.matchMedia = function(q){ return {matches:false,media:q,addListener:function(){},removeListener:function(){},addEventListener:function(){},removeEventListener:function(){}}; };
window.MutationObserver = function(){ this.observe=function(){}; this.disconnect=function(){}; this.takeRecords=function(){return []}; };
window.WebKitMutationObserver = window.MutationObserver;
window.crypto = { getRandomValues:function(a){for(var i=0;i<a.length;i++)a[i]=Math.floor(Math.random()*256);return a}, subtle:{} };
window.Worker = function(){ this.postMessage=function(){}; this.terminate=function(){}; };
window.Blob = function(p,o){ this.size=0; this.type=(o&&o.type)||""; };
window.URL = { createObjectURL:function(){return "blob:mock"}, revokeObjectURL:function(){} };
window.fetch = function(){return null};
window.caches = {}; window.indexedDB = null;
window.localStorage = { getItem:function(){return null}, setItem:function(){}, removeItem:function(){}, clear:function(){}, length:0 };
window.sessionStorage = window.localStorage;
window.history = { length:1, state:null, back:function(){}, forward:function(){}, go:function(){}, pushState:function(){}, replaceState:function(){} };
window.getSelection = function(){ return {rangeCount:0,toString:function(){return ""}}; };
window.focus=function(){}; window.blur=function(){}; window.close=function(){};
window.stop=function(){}; window.print=function(){}; window.alert=function(){};
window.confirm=function(){return false}; window.prompt=function(){return null};
window.scroll=function(){}; window.scrollTo=function(){}; window.scrollBy=function(){};
window.resizeTo=function(){}; window.resizeBy=function(){};
window.moveTo=function(){}; window.moveBy=function(){};
window.open=function(){return null};
window.TextEncoder = function(){}; window.TextEncoder.prototype.encode=function(s){var a=[];for(var i=0;i<s.length;i++)a.push(s.charCodeAt(i));return a};
window.TextDecoder = function(){}; window.TextDecoder.prototype.decode=function(a){var s="";for(var i=0;i<a.length;i++)s+=String.fromCharCode(a[i]);return s};
window.encodeURIComponent=encodeURIComponent; window.decodeURIComponent=decodeURIComponent;
window.encodeURI=encodeURI; window.decodeURI=decodeURI;
window.escape=escape; window.unescape=unescape;
window.isNaN=isNaN; window.isFinite=isFinite; window.parseInt=parseInt; window.parseFloat=parseFloat;
window.JSON=JSON; window.Math=Math; window.Date=Date; window.RegExp=RegExp;
window.Array=Array; window.Object=Object; window.String=String;
window.Number=Number; window.Boolean=Boolean; window.Error=Error;
window.TypeError=TypeError; window.RangeError=RangeError; window.SyntaxError=SyntaxError;
window.ReferenceError=ReferenceError; window.Function=Function;

function __fireEvent(target, type, props) {
    var evt = {type:type, target:target, currentTarget:target, bubbles:true, cancelable:true, isTrusted:true};
    if (props) for (var k in props) evt[k]=props[k];
    var handlers = (target===window) ? __eventHandlers[type] : __docEventHandlers[type];
    if (handlers) for (var i=0; i<handlers.length; i++) {
        try { handlers[i](evt); } catch(e) { __captured.errors.push("fire:"+type+":"+e.toString()); }
    }
    if (typeof target["on"+type]==="function") try { target["on"+type](evt); } catch(e) {}
}

function __drainTimers(max) {
    max=max||100; var n=0;
    while (__timerQueue.length>0 && n<max) {
        var t=__timerQueue.shift();
        try { t.fn(); } catch(e) { __captured.errors.push(e.toString()); }
        if (t.type==="interval") __timerQueue.push(t);
        n++;
    }
}
"""

RUNNER_PHASE1 = """
__fireEvent(document, "DOMContentLoaded", {});
__drainTimers(50);
__fireEvent(window, "load", {});
__drainTimers(200);
JSON.stringify({
    xhr_calls: __captured.xhr_calls,
    cookies: __captured.cookies,
    has_pending: !!__captured.pending_xhr
});
"""

RUNNER_PHASE2 = """
var xhr = __captured.pending_xhr;
if (xhr) {
    xhr.readyState = 4;
    xhr.status = @@STATUS@@;
    xhr.statusText = "OK";
    xhr.responseText = "@@RESPONSE@@";
    if (typeof xhr.onreadystatechange === "function") try { xhr.onreadystatechange(); } catch(e) { __captured.errors.push("cb:" + e.toString()); }
    if (typeof xhr.onload === "function") try { xhr.onload(); } catch(e) { __captured.errors.push("cb:" + e.toString()); }
}
__drainTimers(200);
JSON.stringify({ cookies: __captured.cookies, errors: __captured.errors });
"""
