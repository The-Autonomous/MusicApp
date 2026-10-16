use serde_json::{json, Value};
use std::{
    collections::{BTreeMap, HashMap},
    io::{self, BufRead, BufReader, Write},
    net::{Shutdown, SocketAddr, TcpListener, TcpStream},
    sync::mpsc,
};
use std::{
    fs::File,
    io::{Read, Seek, SeekFrom},
    net::{IpAddr, Ipv4Addr, UdpSocket},
    path::PathBuf,
    sync::{
        atomic::{AtomicBool, AtomicUsize, Ordering},
        mpsc::Sender,
        Arc, Mutex,
    },
    thread,
    time::{Duration, SystemTime, UNIX_EPOCH},
};
use tiny_http::{HTTPVersion, Header, Method, Response, StatusCode};
// tiny_http's listener hides its sockets and exposes no response-write timeout.
// Keep its tested response serializer, but own the bounded listener/connection lifecycle.
struct Server {
    address: SocketAddr,
    stop: Arc<AtomicBool>,
    active: Arc<Mutex<HashMap<usize, TcpStream>>>,
    requests: Mutex<mpsc::Receiver<(usize, TcpStream)>>,
    accept: Mutex<Option<thread::JoinHandle<()>>>,
}
struct Request {
    stream: TcpStream,
    reader: BufReader<TcpStream>,
    headers: Vec<Header>,
    method: Method,
    url: String,
    active: Arc<Mutex<HashMap<usize, TcpStream>>>,
    id: usize,
}
impl Server {
    fn http(address: SocketAddr) -> io::Result<Self> {
        let listener = TcpListener::bind(address)?;
        let address = listener.local_addr()?;
        let stop = Arc::new(AtomicBool::new(false));
        let active: Arc<Mutex<HashMap<usize, TcpStream>>> = Arc::new(Mutex::new(HashMap::new()));
        let (jobs, requests) = mpsc::sync_channel(16);
        let cancel = stop.clone();
        let sockets = active.clone();
        let accept = thread::spawn(move || {
            let mut id = 0usize;
            while !cancel.load(Ordering::Relaxed) {
                let Ok((stream, _)) = listener.accept() else {
                    break;
                };
                if cancel.load(Ordering::Relaxed) {
                    break;
                }
                let _ = stream.set_read_timeout(Some(Duration::from_secs(2)));
                let _ = stream.set_write_timeout(Some(Duration::from_secs(2)));
                if let Ok(copy) = stream.try_clone() {
                    sockets
                        .lock()
                        .unwrap_or_else(|e| e.into_inner())
                        .insert(id, copy);
                }
                if jobs.try_send((id, stream)).is_err() {
                    sockets
                        .lock()
                        .unwrap_or_else(|e| e.into_inner())
                        .remove(&id);
                }
                id = id.wrapping_add(1);
            }
        });
        Ok(Self {
            address,
            stop,
            active,
            requests: Mutex::new(requests),
            accept: Mutex::new(Some(accept)),
        })
    }
    fn shutdown(&self) {
        self.stop.store(true, Ordering::Relaxed);
        for stream in self
            .active
            .lock()
            .unwrap_or_else(|e| e.into_inner())
            .values()
        {
            let _ = stream.shutdown(Shutdown::Both);
        }
        let wake = SocketAddr::new(
            if self.address.ip().is_unspecified() {
                IpAddr::V4(Ipv4Addr::LOCALHOST)
            } else {
                self.address.ip()
            },
            self.address.port(),
        );
        let _ = TcpStream::connect_timeout(&wake, Duration::from_millis(200));
    }
    fn recv_timeout(&self, timeout: Duration) -> io::Result<Option<Request>> {
        let job = self
            .requests
            .lock()
            .unwrap_or_else(|e| e.into_inner())
            .recv_timeout(timeout);
        let (id, stream) = match job {
            Ok(job) => job,
            Err(_) => return Ok(None),
        };
        let result = (|| {
            let mut reader = BufReader::new(stream.try_clone()?);
            let mut bytes = Vec::new();
            loop {
                let before = bytes.len();
                let n = reader
                    .by_ref()
                    .take((16385 - before) as u64)
                    .read_until(b'\n', &mut bytes)?;
                if n == 0 {
                    return Err(io::Error::new(
                        io::ErrorKind::UnexpectedEof,
                        "Incomplete request",
                    ));
                }
                if bytes.len() > 16384 {
                    return Err(io::Error::new(
                        io::ErrorKind::InvalidData,
                        "Header exceeds 16 KiB",
                    ));
                }
                if &bytes[before..] == b"\r\n" || &bytes[before..] == b"\n" {
                    break;
                }
            }
            let text = String::from_utf8_lossy(&bytes);
            let mut lines = text.lines();
            let first: Vec<_> = lines.next().unwrap_or("").split_whitespace().collect();
            if first.len() != 3 {
                return Err(io::Error::new(
                    io::ErrorKind::InvalidData,
                    "Invalid request",
                ));
            }
            let method = first[0]
                .parse::<Method>()
                .map_err(|_| io::Error::new(io::ErrorKind::InvalidData, "Invalid method"))?;
            let url = first[1].to_string();
            let mut headers = Vec::new();
            for line in lines {
                if let Some((key, value)) = line.split_once(':') {
                    if key.eq_ignore_ascii_case("Transfer-Encoding") {
                        return Err(io::Error::new(
                            io::ErrorKind::InvalidData,
                            "Transfer-Encoding unsupported",
                        ));
                    }
                    if let Ok(header) = Header::from_bytes(key.as_bytes(), value.trim().as_bytes())
                    {
                        headers.push(header);
                    }
                }
            }
            if headers
                .iter()
                .find(|h| h.field.equiv("Content-Length"))
                .is_some_and(|h| h.value.as_str().parse::<u64>().map_or(true, |n| n > 65536))
            {
                return Err(io::Error::new(
                    io::ErrorKind::InvalidData,
                    "Body exceeds 64 KiB",
                ));
            }
            Ok(Request {
                stream,
                reader,
                headers,
                method,
                url,
                active: self.active.clone(),
                id,
            })
        })();
        if result.is_err() {
            self.active
                .lock()
                .unwrap_or_else(|e| e.into_inner())
                .remove(&id);
        }
        result.map(Some)
    }
}
impl Drop for Server {
    fn drop(&mut self) {
        self.shutdown();
        if let Some(join) = self.accept.lock().unwrap_or_else(|e| e.into_inner()).take() {
            let _ = join.join();
        }
    }
}
impl Request {
    fn url(&self) -> &str {
        &self.url
    }
    fn headers(&self) -> &[Header] {
        &self.headers
    }
    fn method(&self) -> &Method {
        &self.method
    }
    fn as_reader(&mut self) -> impl Read + '_ {
        let length = self
            .headers
            .iter()
            .find(|h| h.field.equiv("Content-Length"))
            .and_then(|h| h.value.as_str().parse::<u64>().ok())
            .unwrap_or(0);
        self.reader.by_ref().take(length)
    }
    fn respond<R: Read>(mut self, response: Response<R>) -> io::Result<()> {
        response
            .with_header(header("Connection", "close"))
            .raw_print(
                &mut self.stream,
                HTTPVersion(1, 1),
                &self.headers,
                self.method == Method::Head,
                None,
            )?;
        self.stream.flush()
    }
}
impl Drop for Request {
    fn drop(&mut self) {
        let _ = self.stream.shutdown(Shutdown::Both);
        self.active
            .lock()
            .unwrap_or_else(|e| e.into_inner())
            .remove(&self.id);
    }
}
pub fn now() -> f64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap_or_default()
        .as_secs_f64()
}
#[derive(Clone, Default)]
pub struct Status {
    pub title: String,
    pub path: String,
    pub track_id: String,
    pub duration: f64,
    pub position: f64,
    pub paused: bool,
    pub repeat: bool,
    pub volume: f32,
    pub lyrics: Value,
    pub eq: Vec<(u32, f32)>,
    pub library: Vec<(String, String)>,
}
impl Status {
    fn snapshot(&self, route: &str) -> Self {
        Self {
            title: self.title.clone(),
            path: self.path.clone(),
            track_id: self.track_id.clone(),
            duration: self.duration,
            position: self.position,
            paused: self.paused,
            repeat: self.repeat,
            volume: self.volume,
            lyrics: if route == "/lyrics" {
                self.lyrics.clone()
            } else {
                Value::Null
            },
            eq: if route == "/" {
                self.eq.clone()
            } else {
                Vec::new()
            },
            library: if matches!(route, "/search" | "/action") {
                self.library.clone()
            } else {
                Vec::new()
            },
        }
    }
    fn json(&self) -> Value {
        json!({"code":"success","title":self.title,"track_id":self.track_id,"duration":self.duration,"position":self.position,"paused":self.paused,"repeat":self.repeat,"volume":self.volume})
    }
}
pub struct Host {
    stop: Arc<AtomicBool>,
    server: Arc<Server>,
    joins: Vec<thread::JoinHandle<()>>,
    pub status: Arc<Mutex<Status>>,
}
fn header(k: &str, v: &str) -> Header {
    Header::from_bytes(k.as_bytes(), v.as_bytes()).unwrap()
}
fn escaped(s: &str) -> String {
    s.replace('&', "&amp;")
        .replace('<', "&lt;")
        .replace('>', "&gt;")
        .replace('"', "&quot;")
        .replace('\'', "&#x27;")
}
fn decoded(s: &str) -> String {
    s.replace("&lt;", "<")
        .replace("&gt;", ">")
        .replace("&quot;", "\"")
        .replace("&#x27;", "'")
        .replace("&amp;", "&")
}
pub fn local_ip() -> Ipv4Addr {
    UdpSocket::bind("0.0.0.0:0")
        .and_then(|s| {
            s.connect("8.8.8.8:80")?;
            s.local_addr()
        })
        .ok()
        .and_then(|a| match a.ip() {
            IpAddr::V4(a) => Some(a),
            _ => None,
        })
        .unwrap_or(Ipv4Addr::LOCALHOST)
}
#[derive(Default)]
struct LogReader {
    offsets: BTreeMap<usize, u64>,
    size: u64,
    created: Option<SystemTime>,
    modified: Option<SystemTime>,
}
impl LogReader {
    fn chunk(&mut self, path: &std::path::Path, start: usize, count: usize) -> io::Result<Value> {
        let file = File::open(path)?;
        let meta = file.metadata()?;
        let created = meta.created().ok();
        let modified = meta.modified().ok();
        if self.offsets.is_empty()
            || meta.len() < self.size
            || created != self.created
            || (meta.len() == self.size && modified != self.modified)
        {
            self.offsets.clear();
            self.offsets.insert(0, 0);
        }
        self.size = meta.len();
        self.created = created;
        self.modified = modified;
        let (&nearest, &offset) = self.offsets.range(..=start).next_back().unwrap();
        let mut reader = BufReader::new(file);
        reader.seek(SeekFrom::Start(offset))?;
        let mut line = Vec::new();
        let mut current = nearest;
        while current < start {
            line.clear();
            if log_line(&mut reader, &mut line)? == 0 {
                break;
            }
            current += 1;
        }
        let mut lines = Vec::new();
        let mut bytes = 0;
        if current == start {
            while lines.len() < count && bytes < 1024 * 1024 {
                line.clear();
                if log_line(&mut reader, &mut line)? == 0 {
                    break;
                }
                if line.last() == Some(&b'\n') {
                    line.pop();
                }
                bytes += line.len();
                lines.push(String::from_utf8_lossy(&line).into_owned());
            }
            self.offsets
                .insert(start + lines.len(), reader.stream_position()?);
            if self.offsets.len() > 512 {
                self.offsets.clear();
                self.offsets.insert(0, 0);
            }
        }
        Ok(
            json!({"start":start,"count":lines.len(),"has_more":lines.len()==count||bytes>=1024*1024,"lines":lines}),
        )
    }
}
fn log_line<R: BufRead>(reader: &mut R, line: &mut Vec<u8>) -> io::Result<usize> {
    let n = reader.take(65537).read_until(b'\n', line)?;
    if n > 65536 {
        return Err(io::Error::new(
            io::ErrorKind::InvalidData,
            "Log line exceeds 64 KiB",
        ));
    }
    Ok(n)
}
impl Host {
    pub fn start(base: PathBuf, tx: Sender<Vec<String>>) -> Result<Self, String> {
        Self::start_at(base, tx, SocketAddr::from(([0, 0, 0, 0], 8080)))
    }
    pub fn local_addr(&self) -> SocketAddr {
        self.server.address
    }
    pub fn start_at(
        base: PathBuf,
        tx: Sender<Vec<String>>,
        address: SocketAddr,
    ) -> Result<Self, String> {
        let server =
            Arc::new(Server::http(address).map_err(|e| format!("Cannot host on {address}: {e}"))?);
        let port = server.address.port();
        let advertised_ip = if server.address.ip().is_unspecified() {
            IpAddr::V4(local_ip())
        } else {
            server.address.ip()
        };
        let log = Arc::new(Mutex::new(LogReader::default()));
        let stop = Arc::new(AtomicBool::new(false));
        let status = Arc::new(Mutex::new(Status::default()));
        let mut joins = Vec::new();
        for _ in 0..4 {
            let server = server.clone();
            let stop = stop.clone();
            let status = status.clone();
            let base = base.clone();
            let tx = tx.clone();
            let log = log.clone();
            joins.push(thread::spawn(move||{
   while !stop.load(Ordering::Relaxed){let Ok(Some(mut request))=server.recv_timeout(Duration::from_millis(200))else{continue};
    let route=request.url().split('?').next().unwrap_or("").to_string();let snapshot=status.lock().unwrap_or_else(|e|e.into_inner()).snapshot(&route);
    if route=="/song"{
     if let Ok(mut file)=File::open(&snapshot.path){
      let size=file.metadata().map(|m|m.len()).unwrap_or(0);let range=request.headers().iter().find(|h|h.field.equiv("Range")).map(|h|h.value.as_str().to_string());
      let selected=range.as_deref().map(|v|parse_range(v,size));
      if matches!(selected,Some(None)){let _=request.respond(Response::empty(StatusCode(416)).with_header(header("Content-Range",&format!("bytes */{size}"))));continue;}
      let(start,end)=selected.flatten().unwrap_or((0,size.saturating_sub(1)));let length=if size==0{0}else{end-start+1};let _=file.seek(SeekFrom::Start(start));
      let mut headers=vec![header("Accept-Ranges","bytes"),header("Cache-Control","no-store"),header("Content-Type","application/octet-stream")];
      let code=if range.is_some(){headers.push(header("Content-Range",&format!("bytes {start}-{end}/{size}")));206}else{200};
      let _=request.respond(Response::new(StatusCode(code),headers,file.take(length),Some(length as usize),None));
     }else{let _=request.respond(Response::empty(StatusCode(404)));}continue;
    }
    let(mut code,mut content_type)=(200,"text/html; charset=utf-8");
    let body=match route.as_str(){
     "/"=>{let eq=snapshot.eq.iter().map(|(f,g)|format!("{f}:{g}")).collect::<Vec<_>>().join(",");format!("<title>{}</title><track_id>{}</track_id><master_time>{}</master_time><song_start_time>{}</song_start_time><paused>{}</paused><repeat>{}</repeat><eq>{}</eq><volume>{}</volume><location>{}</location><duration>{}</duration><url>http://{}:{}/song?track={}</url><buffered_at>{}</buffered_at><script>location.href='/app_pad';</script>",escaped(&snapshot.title),escaped(&snapshot.track_id),now(),now()-snapshot.position,if snapshot.paused{"True"}else{"False"},if snapshot.repeat{"True"}else{"False"},eq,snapshot.volume,snapshot.position,snapshot.duration,advertised_ip,port,escaped(&snapshot.track_id),now())},
     "/app_pad"|"/app_pad/"=>std::fs::read_to_string(base.join("app_pad/index.html")).unwrap_or_default(),
     "/app_pad/logs"=>std::fs::read_to_string(base.join("app_pad/log.html")).unwrap_or_default(),
     "/time"=>{content_type="text/plain";now().to_string()},
     "/lyrics"=>{content_type="application/json";if snapshot.lyrics.is_array(){snapshot.lyrics.to_string()}else{"[]".into()}},
     "/logs/api"=>{content_type="application/json";let u=url::Url::parse(&format!("http://localhost{}",request.url())).ok();let param=|name:&str,default:usize|->Result<usize,()>{match u.as_ref().and_then(|u|u.query_pairs().find(|(k,_)|k==name)){Some((_,v))=>v.parse().map_err(|_|()),None=>Ok(default)}};match(param("start",0),param("count",100)){(Ok(start),Ok(count))if count>0=>match log.lock().unwrap_or_else(|e|e.into_inner()).chunk(&base.join("state/musicapp.log"),start,count.min(5000)){Ok(value)=>value.to_string(),Err(error)=>{code=if error.kind()==io::ErrorKind::NotFound{404}else{500};json!({"error":error.to_string()}).to_string()}},_=>{code=400;json!({"error":"Invalid start or count"}).to_string()}}},
     "/search"|"/action" if request.method()==&Method::Post=>{
      content_type="application/json";let mut body=String::new();let read=request.as_reader().take(65537).read_to_string(&mut body);
      let data:Value=serde_json::from_str(&body).unwrap_or(Value::Null);
      if read.is_err()||body.len()>65536||!data.is_object(){code=400;json!({"code":"error","message":"Invalid request"}).to_string()}
      else if route=="/search"{let query=data["query"].as_str().unwrap_or("").trim().to_lowercase();if query.is_empty(){code=400;json!({"code":"error","message":"Empty search query"}).to_string()}else{let results:Vec<_>=snapshot.library.iter().filter(|(t,_)|query.split_whitespace().all(|w|t.to_lowercase().contains(w))).take(100).map(|(t,p)|json!({"title":t,"path":p})).collect();json!({"code":"success","results":results}).to_string()}}
      else {let action=data["action"].as_str().unwrap_or("");let mapped=match action{"pause"=>Some("force_pause"),"play"=>Some("force_play"),"skip"=>Some("skip_next"),"previous"=>Some("skip_previous"),"volume_up"=>Some("volume_up"),"volume_down"=>Some("volume_down"),"repeat"=>Some("repeat_toggle"),"play_search"=>Some("remote_play"),"status"=>Some("status"),_=>None};if let Some(a)=mapped{
       let path=data["path"].as_str().unwrap_or("");if a=="remote_play"&&!snapshot.library.iter().any(|(_,p)|p==path){code=400;json!({"code":"error","message":"Unknown library path"}).to_string()}else{let _=tx.send(vec![a.into(),path.into()]);snapshot.json().to_string()}
      }else{code=400;json!({"code":"error","message":"Invalid action"}).to_string()}}
     },
     _=>{code=404;"Not found".into()}
    };
    let _=request.respond(Response::from_string(body).with_status_code(StatusCode(code)).with_header(header("Content-Type",content_type)).with_header(header("Cache-Control","no-store")));
   }
  }));
        }
        Ok(Self {
            stop,
            server,
            joins,
            status,
        })
    }
}
impl Drop for Host {
    fn drop(&mut self) {
        self.stop.store(true, Ordering::Relaxed);
        self.server.shutdown();
        for j in self.joins.drain(..) {
            let _ = j.join();
        }
    }
}
pub fn parse_range(value: &str, size: u64) -> Option<(u64, u64)> {
    if size == 0 {
        return None;
    }
    let s = value.strip_prefix("bytes=")?;
    if s.contains(',') {
        return None;
    }
    let (a, b) = s.split_once('-')?;
    if a.is_empty() {
        let len = b.parse::<u64>().ok()?.min(size);
        return (len > 0).then_some((size - len, size - 1));
    }
    let start = a.parse::<u64>().ok()?;
    let end = if b.is_empty() {
        size - 1
    } else {
        b.parse::<u64>().ok()?.min(size - 1)
    };
    (start < size && start <= end).then_some((start, end))
}
pub fn tag(text: &str, name: &str) -> String {
    let a = format!("<{name}>");
    let b = format!("</{name}>");
    text.split_once(&a)
        .and_then(|(_, s)| s.split_once(&b).map(|(s, _)| decoded(s)))
        .unwrap_or_default()
}
pub struct Client {
    stop: Arc<AtomicBool>,
    join: Option<thread::JoinHandle<()>>,
}
fn client_http(timeout: u64) -> Result<reqwest::blocking::Client, String> {
    reqwest::blocking::Client::builder()
        .connect_timeout(Duration::from_secs(2))
        .timeout(Duration::from_secs(timeout))
        .redirect(reqwest::redirect::Policy::none())
        .build()
        .map_err(|e| e.to_string())
}
fn limited_text(
    mut response: reqwest::blocking::Response,
    cap: u64,
    stop: &AtomicBool,
) -> Result<String, String> {
    let mut bytes = Vec::new();
    let mut block = [0; 8192];
    loop {
        if stop.load(Ordering::Relaxed) {
            return Err("Radio request cancelled".into());
        }
        let n = response.read(&mut block).map_err(|e| e.to_string())?;
        if n == 0 {
            break;
        }
        if bytes.len() as u64 + n as u64 > cap {
            return Err("Radio response too large".into());
        }
        bytes.extend_from_slice(&block[..n]);
    }
    String::from_utf8(bytes).map_err(|e| e.to_string())
}
impl Client {
    pub fn connect(ip: &str, tx: Sender<Vec<String>>) -> Result<Self, String> {
        let parsed: Ipv4Addr = ip.parse().map_err(|_| "Invalid radio address")?;
        Self::connect_at(SocketAddr::new(IpAddr::V4(parsed), 8080), tx)
    }
    pub fn connect_at(address: SocketAddr, tx: Sender<Vec<String>>) -> Result<Self, String> {
        let stop = Arc::new(AtomicBool::new(false));
        let cancel = stop.clone();
        let client = client_http(2)?;
        let join = thread::spawn(move || {
            let mut failures = 0;
            let mut previous = String::new();
            while !cancel.load(Ordering::Relaxed) {
                let started = std::time::Instant::now();
                let result = client
                    .get(format!("http://{address}/"))
                    .send()
                    .and_then(|r| r.error_for_status())
                    .map_err(|e| e.to_string())
                    .and_then(|r| limited_text(r, 64 * 1024, &cancel));
                match result {
                    Ok(text) => {
                        failures = 0;
                        let title = tag(&text, "title");
                        let url = tag(&text, "url");
                        if !title.is_empty() && !url.is_empty() {
                            let id = tag(&text, "track_id");
                            let id = if id.is_empty() { title.clone() } else { id };
                            let mut data = json!({"title":title,"url":url,"track_id":id,"paused":tag(&text,"paused")=="True","repeat":tag(&text,"repeat")=="True","eq":tag(&text,"eq"),"received":now(),"latency":(started.elapsed().as_secs_f64()/2.).min(0.25)});
                            for field in ["duration", "location", "volume"] {
                                data[field] = json!(tag(&text, field).parse::<f64>().unwrap_or(0.));
                            }
                            if !cancel.load(Ordering::Relaxed) {
                                let _ = tx.send(vec!["radio_status".into(), data.to_string()]);
                            }
                            if id != previous && !cancel.load(Ordering::Relaxed) {
                                previous = id.clone();
                                let _ =
                                    tx.send(vec!["radio_lyrics".into(), "[]".into(), id.clone()]);
                                let result = client
                                    .get(format!("http://{address}/lyrics"))
                                    .send()
                                    .and_then(|r| r.error_for_status())
                                    .map_err(|e| e.to_string())
                                    .and_then(|r| limited_text(r, 128 * 1024, &cancel))
                                    .and_then(|raw| lyric_lines(&raw));
                                if let Ok(lyrics) = result {
                                    if !cancel.load(Ordering::Relaxed) {
                                        let _ = tx.send(vec![
                                            "radio_lyrics".into(),
                                            lyrics.to_string(),
                                            id,
                                        ]);
                                    }
                                }
                            }
                        }
                    }
                    Err(error) => {
                        failures += 1;
                        if failures == 1 {
                            let _ = tx.send(vec!["radio_error".into(), error]);
                        }
                    }
                }
                let wait = if failures == 0 {
                    10
                } else {
                    (10u64 << failures.min(4)).min(300)
                };
                for _ in 0..wait {
                    if cancel.load(Ordering::Relaxed) {
                        return;
                    }
                    thread::sleep(Duration::from_millis(100));
                }
            }
        });
        Ok(Self {
            stop,
            join: Some(join),
        })
    }
}
fn lyric_lines(raw: &str) -> Result<Value, String> {
    let value: Value = serde_json::from_str(raw).map_err(|e| e.to_string())?;
    let Some(rows) = value.as_array() else {
        return Err("Radio lyrics must be a list".into());
    };
    let mut lines: Vec<(f64, String)> = rows
        .iter()
        .filter_map(|row| {
            let row = row.as_array()?;
            if row.len() != 2 {
                return None;
            }
            let time = row[0].as_f64().or_else(|| row[0].as_str()?.parse().ok())?;
            if !time.is_finite() {
                return None;
            }
            Some((
                time,
                row[1]
                    .as_str()
                    .map(str::to_string)
                    .unwrap_or_else(|| row[1].to_string()),
            ))
        })
        .collect();
    lines.sort_by(|a, b| a.0.total_cmp(&b.0));
    Ok(json!(lines))
}
impl Drop for Client {
    fn drop(&mut self) {
        self.stop.store(true, Ordering::Relaxed);
        if let Some(j) = self.join.take() {
            let _ = j.join();
        }
    }
}
/// Retain while playing its cached file. Dropping cancels and removes only that file.
pub struct Download {
    stop: Arc<AtomicBool>,
    join: Option<thread::JoinHandle<()>>,
    path: PathBuf,
}
impl Download {
    pub fn start(
        url: &str,
        state_dir: PathBuf,
        identity: String,
        tx: Sender<Vec<String>>,
    ) -> Result<Self, String> {
        let url = url::Url::parse(url).map_err(|e| e.to_string())?;
        if !matches!(url.scheme(), "http" | "https") {
            return Err("Radio URL must use HTTP".into());
        }
        std::fs::create_dir_all(&state_dir).map_err(|e| e.to_string())?;
        let client = client_http(2)?;
        static NEXT: AtomicUsize = AtomicUsize::new(0);
        let path = state_dir.join(format!(
            ".radio-{}-{}-{}.audio",
            std::process::id(),
            SystemTime::now()
                .duration_since(UNIX_EPOCH)
                .unwrap_or_default()
                .as_nanos(),
            NEXT.fetch_add(1, Ordering::Relaxed)
        ));
        let file = std::fs::OpenOptions::new()
            .write(true)
            .create_new(true)
            .open(&path)
            .map_err(|e| e.to_string())?;
        let stop = Arc::new(AtomicBool::new(false));
        let cancel = stop.clone();
        let output = path.clone();
        let join = thread::spawn(move || {
            let result = download_file(&client, url, file, &cancel);
            if cancel.load(Ordering::Relaxed) {
                let _ = std::fs::remove_file(&output);
                return;
            }
            match result {
                Ok(()) => {
                    if tx
                        .send(vec![
                            "radio_download".into(),
                            output.to_string_lossy().into(),
                            identity,
                        ])
                        .is_err()
                    {
                        let _ = std::fs::remove_file(&output);
                    }
                }
                Err(error) => {
                    let _ = std::fs::remove_file(&output);
                    let _ = tx.send(vec!["radio_error".into(), error, identity]);
                }
            }
        });
        Ok(Self {
            stop,
            join: Some(join),
            path,
        })
    }
}
fn download_file(
    client: &reqwest::blocking::Client,
    url: url::Url,
    mut file: File,
    stop: &AtomicBool,
) -> Result<(), String> {
    let mut response = client
        .get(url)
        .send()
        .and_then(|r| r.error_for_status())
        .map_err(|e| e.to_string())?;
    const CAP: u64 = 64 * 1024 * 1024;
    if response.content_length().is_some_and(|n| n > CAP) {
        return Err("Radio song exceeds the 64 MiB cache limit".into());
    }
    let mut buffer = [0; 65536];
    let mut total = 0;
    loop {
        if stop.load(Ordering::Relaxed) {
            return Err("Radio download cancelled".into());
        }
        let n = response.read(&mut buffer).map_err(|e| e.to_string())?;
        if n == 0 {
            break;
        }
        total += n as u64;
        if total > CAP {
            return Err("Radio song exceeds the 64 MiB cache limit".into());
        }
        file.write_all(&buffer[..n]).map_err(|e| e.to_string())?;
    }
    if total == 0 {
        return Err("Radio song is empty".into());
    }
    file.flush().map_err(|e| e.to_string())
}
impl Drop for Download {
    fn drop(&mut self) {
        self.stop.store(true, Ordering::Relaxed);
        if let Some(join) = self.join.take() {
            let _ = join.join();
        }
        let _ = std::fs::remove_file(&self.path);
    }
}
pub struct Scan {
    pub stop: Arc<AtomicBool>,
    joins: Vec<thread::JoinHandle<()>>,
}
impl Scan {
    pub fn start(tx: Sender<Vec<String>>) -> Self {
        let stop = Arc::new(AtomicBool::new(false));
        let next = Arc::new(AtomicUsize::new(1));
        let ip = local_ip().octets();
        let mut joins = Vec::new();
        for _ in 0..4 {
            let stop = stop.clone();
            let next = next.clone();
            let tx = tx.clone();
            joins.push(thread::spawn(move || {
                let Ok(client) = client_http(2) else { return };
                loop {
                    if stop.load(Ordering::Relaxed) {
                        return;
                    }
                    let n = next.fetch_add(1, Ordering::Relaxed);
                    if n >= 255 {
                        return;
                    }
                    if n as u8 == ip[3] {
                        continue;
                    }
                    let addr = Ipv4Addr::new(ip[0], ip[1], ip[2], n as u8);
                    if let Ok(text) = client
                        .get(format!("http://{addr}:8080/"))
                        .send()
                        .and_then(|r| r.error_for_status())
                        .map_err(|e| e.to_string())
                        .and_then(|r| limited_text(r, 64 * 1024, &stop))
                    {
                        if !tag(&text, "title").is_empty() {
                            let _ = tx.send(vec![
                                "radio_found".into(),
                                addr.to_string(),
                                tag(&text, "title"),
                            ]);
                        }
                    }
                }
            }));
        }
        Self { stop, joins }
    }
}
impl Drop for Scan {
    fn drop(&mut self) {
        self.stop.store(true, Ordering::Relaxed);
        for j in self.joins.drain(..) {
            let _ = j.join();
        }
    }
}
#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn ranges() {
        assert_eq!(parse_range("bytes=2-4", 10), Some((2, 4)));
        assert_eq!(parse_range("bytes=-3", 10), Some((7, 9)));
        assert_eq!(parse_range("bytes=10-", 10), None);
        assert_eq!(parse_range("bytes=4-1", 10), None);
    }
    #[test]
    fn html_roundtrip() {
        let s = "A <song> & \"B\"";
        assert_eq!(tag(&format!("<title>{}</title>", escaped(s)), "title"), s);
    }
}
#[cfg(test)]
mod integration_tests {
    use super::*;
    use std::time::Instant;
    struct Fixture(PathBuf);
    impl Fixture {
        fn new() -> Self {
            static NEXT: AtomicUsize = AtomicUsize::new(0);
            let path = PathBuf::from(env!("CARGO_MANIFEST_DIR"))
                .join("target/radio-tests")
                .join(format!(
                    "{}-{}",
                    std::process::id(),
                    NEXT.fetch_add(1, Ordering::Relaxed)
                ));
            std::fs::create_dir_all(path.join("state")).unwrap();
            std::fs::create_dir_all(path.join("app_pad")).unwrap();
            std::fs::write(path.join("app_pad/index.html"), "<h1>Phone</h1>").unwrap();
            std::fs::write(path.join("app_pad/log.html"), "<h1>Logs</h1>").unwrap();
            Self(path)
        }
    }
    impl Drop for Fixture {
        fn drop(&mut self) {
            let _ = std::fs::remove_dir_all(&self.0);
        }
    }
    fn host(f: &Fixture) -> (Host, mpsc::Receiver<Vec<String>>) {
        let (tx, rx) = mpsc::channel();
        (
            Host::start_at(f.0.clone(), tx, "127.0.0.1:0".parse().unwrap()).unwrap(),
            rx,
        )
    }
    #[test]
    fn loopback_endpoints_controls_search_ranges_and_logs() {
        let f = Fixture::new();
        let song = f.0.join("song.wav");
        std::fs::write(&song, b"0123456789").unwrap();
        std::fs::write(f.0.join("state/musicapp.log"), "first\nsecond\nthird\n").unwrap();
        let (host, rx) = host(&f);
        let base = format!("http://{}", host.local_addr());
        let client = client_http(2).unwrap();
        {
            let mut state = host.status.lock().unwrap();
            state.title = "Artist <song> & title".into();
            state.track_id = "song-1".into();
            state.path = song.to_string_lossy().into();
            state.library = vec![("Artist Song".into(), state.path.clone())];
            state.lyrics = json!([[0, "Hello"]]);
        }
        let text = client.get(&base).send().unwrap().text().unwrap();
        assert_eq!(tag(&text, "title"), "Artist <song> & title");
        assert!(tag(&text, "url").contains(&host.local_addr().port().to_string()));
        for (route, expected) in [
            ("/app_pad", "<h1>Phone</h1>"),
            ("/app_pad/logs", "<h1>Logs</h1>"),
            ("/lyrics", "[[0,\"Hello\"]]"),
        ] {
            assert_eq!(
                client
                    .get(format!("{base}{route}"))
                    .send()
                    .unwrap()
                    .text()
                    .unwrap(),
                expected
            );
        }
        assert!(
            client
                .get(format!("{base}/time"))
                .send()
                .unwrap()
                .text()
                .unwrap()
                .parse::<f64>()
                .unwrap()
                > 0.
        );
        let response = client
            .get(format!("{base}/song"))
            .header("Range", "bytes=2-4")
            .send()
            .unwrap();
        assert_eq!(response.status(), 206);
        assert_eq!(response.headers()["content-range"], "bytes 2-4/10");
        assert_eq!(response.text().unwrap(), "234");
        assert_eq!(
            client
                .get(format!("{base}/song"))
                .header("Range", "bytes=100-")
                .send()
                .unwrap()
                .status(),
            416
        );
        assert_eq!(
            client
                .get(format!("{base}/song"))
                .header("Range", "bytes=-3")
                .send()
                .unwrap()
                .text()
                .unwrap(),
            "789"
        );
        assert_eq!(
            client
                .head(format!("{base}/song"))
                .send()
                .unwrap()
                .headers()["content-length"],
            "10"
        );
        let found: Value = client
            .post(format!("{base}/search"))
            .json(&json!({"query":"artist song"}))
            .send()
            .unwrap()
            .json()
            .unwrap();
        assert_eq!(found["results"].as_array().unwrap().len(), 1);
        for (action, expected) in [
            ("pause", "force_pause"),
            ("play", "force_play"),
            ("skip", "skip_next"),
            ("previous", "skip_previous"),
            ("repeat", "repeat_toggle"),
            ("volume_up", "volume_up"),
            ("volume_down", "volume_down"),
        ] {
            assert!(client
                .post(format!("{base}/action"))
                .json(&json!({"action":action}))
                .send()
                .unwrap()
                .status()
                .is_success());
            assert_eq!(
                rx.recv_timeout(Duration::from_secs(1)).unwrap()[0],
                expected
            );
        }
        assert_eq!(
            client
                .post(format!("{base}/action"))
                .json(&json!({"action":"play_search","path":"outside"}))
                .send()
                .unwrap()
                .status(),
            400
        );
        let logs: Value = client
            .get(format!("{base}/logs/api?start=1&count=1"))
            .send()
            .unwrap()
            .json()
            .unwrap();
        assert_eq!(
            logs,
            json!({"lines":["second"],"start":1,"count":1,"has_more":true})
        );
        let logs: Value = client
            .get(format!("{base}/logs/api?start=2&count=3"))
            .send()
            .unwrap()
            .json()
            .unwrap();
        assert_eq!(logs["lines"], json!(["third"]));
        assert_eq!(logs["has_more"], false);
        for query in ["start=-1", "count=0", "count=x"] {
            assert_eq!(
                client
                    .get(format!("{base}/logs/api?{query}"))
                    .send()
                    .unwrap()
                    .status(),
                400
            );
        }
        std::fs::write(f.0.join("state/musicapp.log"), "rotated\n").unwrap();
        let logs: Value = client
            .get(format!("{base}/logs/api?start=0&count=1"))
            .send()
            .unwrap()
            .json()
            .unwrap();
        assert_eq!(logs["lines"], json!(["rotated"]));
    }
    #[test]
    fn shutdown_interrupts_non_reading_client_and_partial_post() {
        let f = Fixture::new();
        let song = f.0.join("huge.wav");
        File::create(&song)
            .unwrap()
            .set_len(128 * 1024 * 1024)
            .unwrap();
        let (host, _) = host(&f);
        host.status.lock().unwrap().path = song.to_string_lossy().into();
        let mut non_reader = TcpStream::connect(host.local_addr()).unwrap();
        non_reader
            .write_all(b"GET /song HTTP/1.1\r\nHost: localhost\r\n\r\n")
            .unwrap();
        let mut partial = TcpStream::connect(host.local_addr()).unwrap();
        partial
            .write_all(
                b"POST /action HTTP/1.1\r\nHost: localhost\r\nContent-Length: 65536\r\n\r\n{",
            )
            .unwrap();
        thread::sleep(Duration::from_millis(100));
        let started = Instant::now();
        drop(host);
        // Windows can retain a synchronous Winsock write until its 2s deadline
        // even after shutdown on a duplicate handle. Joining remains bounded.
        assert!(
            started.elapsed() < Duration::from_secs(3),
            "shutdown {:?}",
            started.elapsed()
        );
    }
    #[test]
    fn lyrics_sorted_on_track_change_and_oversized_reply_discarded() {
        let f = Fixture::new();
        let (host, _) = host(&f);
        {
            let mut state = host.status.lock().unwrap();
            state.title = "Song".into();
            state.track_id = "one".into();
            state.lyrics = json!([[4, "later"], [1, "earlier"]]);
        }
        let (tx, rx) = mpsc::channel();
        let client = Client::connect_at(host.local_addr(), tx).unwrap();
        let until = Instant::now() + Duration::from_secs(3);
        let mut found = false;
        while Instant::now() < until {
            let event = rx.recv_timeout(Duration::from_secs(1)).unwrap();
            if event[0] == "radio_lyrics" && event[1] != "[]" {
                assert_eq!(
                    serde_json::from_str::<Value>(&event[1]).unwrap(),
                    json!([[1.0, "earlier"], [4.0, "later"]])
                );
                found = true;
                break;
            }
        }
        assert!(found);
        {
            let mut state = host.status.lock().unwrap();
            state.track_id = "two".into();
            state.lyrics = json!([[0, "x".repeat(129 * 1024)]]);
        }
        let until = Instant::now() + Duration::from_secs(3);
        let mut cleared = false;
        while Instant::now() < until {
            let event = rx.recv_timeout(Duration::from_secs(2)).unwrap();
            if event[0] == "radio_lyrics" && event[2] == "two" {
                assert_eq!(event[1], "[]");
                cleared = true;
                break;
            }
        }
        assert!(cleared);
        drop(client);
    }
    #[test]
    fn cancel_stalled_fallback_removes_partial_file() {
        let f = Fixture::new();
        let listener = TcpListener::bind("127.0.0.1:0").unwrap();
        let address = listener.local_addr().unwrap();
        let stopped = Arc::new(AtomicBool::new(false));
        let cancel = stopped.clone();
        let (ready_tx, ready_rx) = mpsc::channel();
        let server = thread::spawn(move || {
            let (mut socket, _) = listener.accept().unwrap();
            socket
                .set_read_timeout(Some(Duration::from_secs(2)))
                .unwrap();
            let mut request = [0; 4096];
            let _ = socket.read(&mut request);
            socket.write_all(b"HTTP/1.1 200 OK\r\nContent-Length: 1000000\r\nConnection: close\r\n\r\npartial").unwrap();
            let _ = ready_tx.send(());
            while !cancel.load(Ordering::Relaxed) {
                thread::sleep(Duration::from_millis(10));
            }
        });
        let (tx, _rx) = mpsc::channel();
        let download = Download::start(
            &format!("http://{address}/song"),
            f.0.join("state"),
            "cancel".into(),
            tx,
        )
        .unwrap();
        ready_rx.recv_timeout(Duration::from_secs(2)).unwrap();
        let started = Instant::now();
        drop(download);
        stopped.store(true, Ordering::Relaxed);
        server.join().unwrap();
        assert!(started.elapsed() < Duration::from_secs(3));
        assert!(!std::fs::read_dir(f.0.join("state")).unwrap().any(|p| p
            .unwrap()
            .file_name()
            .to_string_lossy()
            .starts_with(".radio-")));
    }
    #[test]
    fn fallback_download_limit_and_owned_cleanup() {
        let f = Fixture::new();
        let song = f.0.join("song.wav");
        std::fs::write(&song, b"audio data").unwrap();
        let (host, _) = host(&f);
        host.status.lock().unwrap().path = song.to_string_lossy().into();
        let (tx, rx) = mpsc::channel();
        let download = Download::start(
            &format!("http://{}/song", host.local_addr()),
            f.0.join("state"),
            "one".into(),
            tx.clone(),
        )
        .unwrap();
        let event = rx.recv_timeout(Duration::from_secs(3)).unwrap();
        assert_eq!(event[0], "radio_download");
        assert_eq!(event[2], "one");
        let path = PathBuf::from(&event[1]);
        assert_eq!(std::fs::read(&path).unwrap(), b"audio data");
        drop(download);
        assert!(!path.exists());
        File::create(&song)
            .unwrap()
            .set_len(64 * 1024 * 1024 + 1)
            .unwrap();
        let download = Download::start(
            &format!("http://{}/song", host.local_addr()),
            f.0.join("state"),
            "two".into(),
            tx,
        )
        .unwrap();
        let event = rx.recv_timeout(Duration::from_secs(3)).unwrap();
        assert_eq!(event[0], "radio_error");
        assert_eq!(event[2], "two");
        drop(download);
        assert!(!std::fs::read_dir(f.0.join("state")).unwrap().any(|p| p
            .unwrap()
            .file_name()
            .to_string_lossy()
            .starts_with(".radio-")));
    }
}
