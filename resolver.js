/*
 * 净影 · 页面内解析探针（可重复注入）
 *
 * 抖音页面初始化时会替换 window.fetch / XMLHttpRequest。探针每次注入都会检查
 * 当前实现是否已经被我们包过；如果页面换掉了，就重新包一层。这样即使在页面
 * 初始化前后多次注入，也能拿到详情接口的响应。
 */
(function () {
    var state = window.__JY_STATE__;
    if (!state) {
        state = window.__JY_STATE__ = {
            detail: '',
            play: '',
            meta: {},
            logs: []
        };
    }

    function bridge(name, value) {
        try {
            if (window.JY && typeof window.JY[name] === 'function') {
                window.JY[name](String(value == null ? '' : value));
            }
        } catch (e) {
            // ignore
        }
    }

    function log(message) {
        try {
            state.logs.push(String(message).slice(0, 300));
            if (state.logs.length > 30) {
                state.logs.shift();
            }
            bridge('onLog', message);
        } catch (e) {
            // ignore
        }
    }

    function normalizeUrl(url) {
        if (!url) {
            return '';
        }
        var value = String(url).trim();
        if (value.indexOf('http://') === 0) {
            value = 'https://' + value.substring(7);
        }
        value = value.replace('/playwm/', '/play/').replace('playwm?', 'play?');
        return value;
    }

    function pageAwemeId() {
        try {
            var match = String(location.pathname || '').match(/\/(?:video|note|share\/video)\/(\d{15,25})/);
            if (match) {
                return match[1];
            }
            match = String(location.search || '').match(/[?&](?:modal_id|aweme_id|item_id)=(\d{15,25})/);
            return match ? match[1] : '';
        } catch (e) {
            return '';
        }
    }

    function pushDetail(value) {
        try {
            var text = typeof value === 'string' ? value : JSON.stringify(value);
            if (!text || text.length < 40 || text.indexOf('aweme_detail') < 0) {
                return;
            }
            var expectedId = pageAwemeId();
            if (expectedId) {
                var idMatch = text.match(/"aweme_id"\s*:\s*"(\d{15,25})"/);
                if (idMatch && idMatch[1] !== expectedId) {
                    return;
                }
            }
            if (state.detail && state.detail.length >= text.length) {
                return;
            }
            state.detail = text;
            bridge('onDetail', text);
        } catch (e) {
            log('detail parse failed: ' + e);
        }
    }

    function pushPlay(url) {
        try {
            var value = normalizeUrl(url);
            if (!value || value.indexOf('http') !== 0) {
                return;
            }
            if (value.indexOf('/aweme/v1/play/') < 0 &&
                value.indexOf('douyinvod.com') < 0 &&
                value.indexOf('/aweme/v1/playwm/') < 0) {
                return;
            }
            if (state.play === value) {
                return;
            }
            state.play = value;
            bridge('onPlay', value);
        } catch (e) {
            // ignore
        }
    }

    function pushProfilePage(value, url) {
        try {
            if (url && String(url).indexOf('/aweme/post') < 0) {
                return;
            }
            var text = typeof value === 'string' ? value : JSON.stringify(value);
            if (!text || text.indexOf('"aweme_list"') < 0) {
                return;
            }
            if (state.lastProfileText === text) {
                return;
            }
            state.lastProfileText = text;
            state.profilePages = (state.profilePages || 0) + 1;
            bridge('onPostPage', text);
        } catch (e) {
            log('post page parse failed: ' + e);
        }
    }

    function pushProfileUser(value, url) {
        try {
            if (url && String(url).indexOf('/user/profile/other') < 0) {
                return;
            }
            var text = typeof value === 'string' ? value : JSON.stringify(value);
            if (!text || text.indexOf('"user"') < 0) {
                return;
            }
            if (state.lastProfileUserText === text) {
                return;
            }
            state.lastProfileUserText = text;
            bridge('onProfileUser', text);
        } catch (e) {
            // ignore
        }
    }

    /*
     * 直接读 Response.prototype.json/text 的返回值，而不是 clone()。
     * 抖音自己的 fetch 包装会先消费响应体，clone 会报 "Response body is already used"。
     * 观察返回值不会改变或消费页面拿到的数据。
     */
    function installResponseHook() {
        try {
            var proto = Response.prototype;
            if (!proto.json.__JY_WRAPPED__) {
                var originalJson = proto.json;
                var wrappedJson = function () {
                    var response = this;
                    var promise = originalJson.apply(response, arguments);
                    try {
                        var u = String(response.url || '');
                        var wantsDetail = !u || u.indexOf('/aweme/detail/') >= 0;
                        var wantsPost = !u || u.indexOf('/aweme/post') >= 0;
                        var wantsUser = !u || u.indexOf('/user/profile/other') >= 0;
                        promise.then(function (data) {
                            if (wantsDetail) {
                                pushDetail(data);
                            }
                            if (wantsPost) {
                                pushProfilePage(data, u);
                            }
                            if (wantsUser) {
                                pushProfileUser(data, u);
                            }
                        }).catch(function () {
                        });
                    } catch (e) {
                        // ignore
                    }
                    return promise;
                };
                wrappedJson.__JY_WRAPPED__ = true;
                proto.json = wrappedJson;
            }
            if (!proto.text.__JY_WRAPPED__) {
                var originalText = proto.text;
                var wrappedText = function () {
                    var response = this;
                    var promise = originalText.apply(response, arguments);
                    try {
                        var u = String(response.url || '');
                        var wantsDetail = !u || u.indexOf('/aweme/detail/') >= 0;
                        var wantsPost = !u || u.indexOf('/aweme/post') >= 0;
                        var wantsUser = !u || u.indexOf('/user/profile/other') >= 0;
                        promise.then(function (text) {
                            if (wantsDetail) {
                                pushDetail(text);
                            }
                            if (wantsPost) {
                                pushProfilePage(text, u);
                            }
                            if (wantsUser) {
                                pushProfileUser(text, u);
                            }
                        }).catch(function () {
                        });
                    } catch (e) {
                        // ignore
                    }
                    return promise;
                };
                wrappedText.__JY_WRAPPED__ = true;
                proto.text = wrappedText;
            }
        } catch (e) {
            log('response hook failed: ' + e);
        }
    }

    function installXhrHook() {
        try {
            var proto = XMLHttpRequest.prototype;
            if (proto.open.__JY_WRAPPED__) {
                return;
            }
            var originalOpen = proto.open;
            var wrappedOpen = function (method, url) {
                var xhr = this;
                try {
                    var requestUrl = String(url || '');
                    if (requestUrl.indexOf('/aweme/detail/') >= 0
                            || requestUrl.indexOf('/aweme/post') >= 0
                            || requestUrl.indexOf('/user/profile/other') >= 0) {
                        xhr.addEventListener('load', function () {
                            try {
                                var value = xhr.responseType === 'json' && xhr.response
                                    ? xhr.response : xhr.responseText;
                                if (requestUrl.indexOf('/aweme/detail/') >= 0) {
                                    pushDetail(value);
                                }
                                if (requestUrl.indexOf('/aweme/post') >= 0) {
                                    pushProfilePage(value, requestUrl);
                                }
                                if (requestUrl.indexOf('/user/profile/other') >= 0) {
                                    pushProfileUser(value, requestUrl);
                                }
                            } catch (e) {
                                // ignore
                            }
                        });
                    }
                } catch (e) {
                    // ignore
                }
                return originalOpen.apply(this, arguments);
            };
            wrappedOpen.__JY_WRAPPED__ = true;
            proto.open = wrappedOpen;
        } catch (e) {
            log('xhr hook failed: ' + e);
        }
    }

    function scan() {
        try {
            var videos = document.querySelectorAll('video');
            for (var i = 0; i < videos.length; i++) {
                var video = videos[i];
                if (video.currentSrc) {
                    pushPlay(video.currentSrc);
                }
                if (video.src) {
                    pushPlay(video.src);
                }
                if (video.poster && !state.meta.poster) {
                    state.meta.poster = video.poster;
                }
                if (video.duration && isFinite(video.duration) && !state.meta.duration) {
                    state.meta.duration = Math.round(video.duration * 1000);
                }
                if (video.currentSrc && !state.meta.videoSrc) {
                    state.meta.videoSrc = video.currentSrc;
                }
                // 页面播放器可能自行取消静音；每次扫描都强制静音，避免隐藏页面发出声音。
                try {
                    video.muted = true;
                    video.volume = 0;
                } catch (volumeError) {
                    // ignore
                }
                if (video.paused) {
                    try {
                        var result = video.play();
                        if (result && result.catch) {
                            result.catch(function () {
                            });
                        }
                    } catch (e) {
                        // autoplay may be blocked
                    }
                }
            }
            var audios = document.querySelectorAll('audio');
            for (var j = 0; j < audios.length; j++) {
                try {
                    audios[j].muted = true;
                    audios[j].volume = 0;
                } catch (audioError) {
                    // ignore
                }
            }

            state.meta.pageTitle = document.title || '';
            state.meta.url = location.href || '';
            var desc = document.querySelector('meta[name="description"]');
            if (desc && desc.content) {
                state.meta.description = desc.content;
            }
            var ogTitle = document.querySelector('meta[property="og:title"]');
            if (ogTitle && ogTitle.content) {
                state.meta.ogTitle = ogTitle.content;
            }
            var ogImage = document.querySelector('meta[property="og:image"]');
            if (ogImage && ogImage.content) {
                state.meta.ogImage = ogImage.content;
            }
            var author = document.querySelector('[data-e2e="video-author-name"]');
            if (author && author.textContent) {
                state.meta.author = author.textContent.trim();
            }
        } catch (e) {
            // ignore
        }
    }

    installResponseHook();
    installXhrHook();

    try {
        if (!window.__JY_OBSERVER__ && window.PerformanceObserver) {
            window.__JY_OBSERVER__ = true;
            var observer = new PerformanceObserver(function (list) {
                list.getEntries().forEach(function (entry) {
                    pushPlay(entry.name);
                });
            });
            observer.observe({entryTypes: ['resource']});
        }
    } catch (e) {
        log('performance observer failed: ' + e);
    }

    window.__JY_SUMMARY__ = function () {
        installResponseHook();
        installXhrHook();
        scan();
        return JSON.stringify({
            hasDetail: !!state.detail,
            play: state.play || '',
            meta: state.meta || {}
        });
    };

    window.__JY_DETAIL__ = function () {
        return state.detail || '';
    };

    window.__JY_MUTE__ = function () {
        try {
            var media = document.querySelectorAll('video,audio');
            for (var i = 0; i < media.length; i++) {
                try {
                    media[i].muted = true;
                    media[i].volume = 0;
                    media[i].pause();
                } catch (mediaError) {
                    // ignore
                }
            }
            if (window.speechSynthesis && window.speechSynthesis.cancel) {
                window.speechSynthesis.cancel();
            }
            return 'ok';
        } catch (e) {
            return 'err';
        }
    };

    window.__JY_SCROLL__ = function () {
        try {
            var root = document.scrollingElement || document.documentElement;
            var height = Math.max(root.scrollHeight, document.body.scrollHeight);
            window.scrollTo(0, height);
            root.scrollTop = height;
            document.body.scrollTop = height;

            // 抖音主页有时使用内部滚动容器而不是 window，逐层找可滚动元素并推到底。
            var containers = document.querySelectorAll('main,div,section');
            for (var i = 0; i < containers.length; i++) {
                var el = containers[i];
                try {
                    if (el.clientHeight > 240 && el.scrollHeight > el.clientHeight + 80) {
                        el.scrollTop = el.scrollHeight;
                        if (el.scrollHeight > height) {
                            height = el.scrollHeight;
                        }
                        el.dispatchEvent(new Event('scroll', {bubbles: true}));
                    }
                } catch (containerError) {
                    // ignore
                }
            }
            try {
                window.dispatchEvent(new Event('scroll'));
                var target = document.elementFromPoint(
                    Math.floor(window.innerWidth / 2), Math.floor(window.innerHeight * 0.7));
                if (target) {
                    target.dispatchEvent(new WheelEvent('wheel', {
                        deltaY: 1200, bubbles: true, cancelable: true
                    }));
                }
            } catch (eventError) {
                // ignore
            }
            return height;
        } catch (e) {
            return 0;
        }
    };

    window.__JY_REFETCH__ = function (url, headersJson) {
        try {
            if (!url || typeof window.fetch !== 'function') {
                return;
            }
            var headers = {};
            try {
                headers = JSON.parse(headersJson || '{}');
            } catch (e) {
                headers = {};
            }
            ['cookie', 'host', 'content-length', 'accept-encoding', 'connection',
                'user-agent', 'referer', 'origin', 'sec-fetch-site', 'sec-fetch-mode',
                'sec-fetch-dest'].forEach(function (key) {
                delete headers[key];
                delete headers[key.toLowerCase()];
            });
            window.fetch(url, {
                method: 'GET',
                credentials: 'include',
                cache: 'no-store',
                headers: headers
            }).then(function (response) {
                log('refetch http ' + response.status);
                return response.text().then(function (text) {
                    pushDetail(text);
                });
            }).catch(function (error) {
                log('refetch failed ' + error);
            });
        } catch (e) {
            log('refetch exception ' + e);
        }
    };

    window.__JY_HOOKED__ = true;
    scan();
    if (!window.__JY_INTERVAL__) {
        window.__JY_INTERVAL__ = setInterval(function () {
            installResponseHook();
            installXhrHook();
            scan();
        }, 1200);
    }
    if (!state.hookLoggedAt || Date.now() - state.hookLoggedAt > 3000) {
        state.hookLoggedAt = Date.now();
        log('hook installed f=' + (Response.prototype.json.__JY_WRAPPED__ ? '1' : '0')
            + ' u=' + (location.href || '').slice(0, 120));
    }
})();
