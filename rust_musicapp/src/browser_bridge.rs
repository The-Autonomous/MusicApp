//! On-demand localhost handoff from a user-clicked Chrome/Edge extension.
//! No daemon, profile decryption, browser injection, or credential logging.
use rand::RngCore;
use serde_json::Value;
use std::{collections::HashMap, io::{Read, Write}, net::{TcpListener, TcpStream}, time::{Duration, Instant}};

const MAX_BODY: usize = 256 * 1024;
const PAGE: &str = include_str!("../browser_connector/connect.html");

fn extension_origin(origin: &str) -> bool {
    origin.strip_prefix("chrome-extension://").is_some_and(|id|id.len()==32 && id.bytes().all(|b|(b'a'..=b'p').contains(&b)))
}
fn reply(stream: &mut TcpStream, status: &str, body: &str, origin: Option<&str>, html: bool) {
    let cors = origin.filter(|v|extension_origin(v)).map(|v|format!("Access-Control-Allow-Origin: {v}\r\nVary: Origin\r\nAccess-Control-Allow-Methods: POST, OPTIONS\r\nAccess-Control-Allow-Headers: Content-Type, X-MusicApp-Token\r\nAccess-Control-Allow-Private-Network: true\r\n")).unwrap_or_default();
    let mime=if html {"text/html; charset=utf-8"} else {"application/json"};
    let head=format!("HTTP/1.1 {status}\r\nContent-Type: {mime}\r\nContent-Length: {}\r\nCache-Control: no-store\r\nContent-Security-Policy: default-src 'none'; style-src 'unsafe-inline'; frame-ancestors 'none'\r\nReferrer-Policy: no-referrer\r\nConnection: close\r\n{cors}\r\n",body.len());
    let _=stream.write_all(head.as_bytes()).and_then(|_|stream.write_all(body.as_bytes()));
}
fn handle<F>(stream: &mut TcpStream, host: &str, token: &str, apply: &mut F) -> Result<Option<Value>, String>
where F:FnMut(Value)->Result<Value,String> {
    stream.set_read_timeout(Some(Duration::from_secs(2))).map_err(|e|e.to_string())?;
    stream.set_write_timeout(Some(Duration::from_secs(2))).map_err(|e|e.to_string())?;
    let mut input=Vec::new();
    let received_at=Instant::now();
    let end=loop {
        if received_at.elapsed()>Duration::from_secs(3) { return Ok(None); }
        let mut byte=[0];
        if stream.read(&mut byte).map_err(|_|"Incomplete browser connection")?==0 {return Ok(None)}
        input.push(byte[0]);
        if input.ends_with(b"\r\n\r\n") {break input.len()}
        if input.len()>8192 {reply(stream,"431 Request Header Fields Too Large","{}",None,false);return Ok(None)}
    };
    let header=std::str::from_utf8(&input[..end]).map_err(|_|"Invalid browser request")?;
    let mut lines=header.split("\r\n");
    let mut request=lines.next().unwrap_or("").split_whitespace();
    let method=request.next().unwrap_or(""); let path=request.next().unwrap_or("");
    let headers:HashMap<_,_>=lines.filter_map(|line|line.split_once(':')).map(|(k,v)|(k.to_ascii_lowercase(),v.trim().to_owned())).collect();
    if headers.get("host").map(String::as_str)!=Some(host) {
        reply(stream,"403 Forbidden","{}",None,false); return Ok(None)
    }
    if method=="GET" && path=="/connect" {reply(stream,"200 OK",PAGE,None,true);return Ok(None)}
    let origin=headers.get("origin").map(String::as_str).filter(|v|extension_origin(v));
    if path!="/session" || origin.is_none() {reply(stream,"403 Forbidden","{}",None,false);return Ok(None)}
    if method=="OPTIONS" {reply(stream,"204 No Content","",origin,false);return Ok(None)}
    if method!="POST" || headers.get("x-musicapp-token").map(String::as_str)!=Some(token) {
        reply(stream,"403 Forbidden","{}",origin,false);return Ok(None)
    }
    let length=headers.get("content-length").and_then(|v|v.parse::<usize>().ok()).filter(|&v|v>0 && v<=MAX_BODY);
    let Some(length)=length else {reply(stream,"413 Content Too Large","{}",origin,false);return Ok(None)};
    if headers.contains_key("transfer-encoding") {reply(stream,"400 Bad Request","{}",origin,false);return Ok(None)}
    let mut body=vec![0;length];
    let mut received=0;
    while received<length {
        if received_at.elapsed()>Duration::from_secs(5) {return Ok(None)}
        let count=stream.read(&mut body[received..]).map_err(|_|"Incomplete browser session handoff")?;
        if count==0 {return Ok(None)}
        received+=count;
    }
    let cookies=serde_json::from_slice::<Value>(&body).map_err(|_|"Invalid browser session handoff")?;
    match apply(cookies) {
        Ok(report)=>{reply(stream,"200 OK","{\"verified\":true}",origin,false);Ok(Some(report))},
        Err(_)=>{reply(stream,"422 Unprocessable Content","{\"verified\":false,\"error\":\"YouTube did not accept this session. Sign in to YouTube in this browser profile and retry.\"}",origin,false);Ok(None)}
    }
}
pub fn connect<F>(mut apply:F)->Result<Value,String> where F:FnMut(Value)->Result<Value,String> {
    let listener=TcpListener::bind("127.0.0.1:0").map_err(|_|"Cannot start local browser connection")?;
    listener.set_nonblocking(true).map_err(|e|e.to_string())?;
    let host=listener.local_addr().map_err(|e|e.to_string())?.to_string();
    let mut bytes=[0u8;32]; rand::thread_rng().fill_bytes(&mut bytes);
    let token=bytes.iter().map(|v|format!("{v:02x}")).collect::<String>();
    crate::services::open_browser_profile_page(&format!("http://{host}/connect#musicapp={token}"))?;
    let deadline=Instant::now()+Duration::from_secs(300);
    while Instant::now()<deadline {
        match listener.accept() {
            Ok((mut stream,address)) if address.ip().is_loopback()=> {
                if let Ok(Some(report))=handle(&mut stream,&host,&token,&mut apply) {return Ok(report)}
            },
            Err(error) if error.kind()==std::io::ErrorKind::WouldBlock=>std::thread::sleep(Duration::from_millis(100)),
            Ok(_)=>{}, Err(_)=>return Err("Local browser connection stopped".into())
        }
    }
    Err("Browser connection timed out. Run connect-browser.bat and click Connect in the MusicApp browser extension.".into())
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;
    #[test] fn origins_are_extension_only() {
        assert!(extension_origin("chrome-extension://abcdefghijklmnopabcdefghijklmnop"));
        for origin in ["https://youtube.com","http://127.0.0.1","chrome-extension://bad","chrome-extension://abcdefghijklmnopabcdefghijklmnop/evil"] {assert!(!extension_origin(origin));}
    }
    fn exchange(headers:&str)->(String,bool) {
        let listener=TcpListener::bind("127.0.0.1:0").unwrap(); let address=listener.local_addr().unwrap();
        let request=headers.replace("HOST",&address.to_string());
        let task=std::thread::spawn(move||{let(mut stream,_)=listener.accept().unwrap(); let mut called=false; let _=handle(&mut stream,&address.to_string(),"secret",&mut|_|{called=true;Ok(json!({"verified":true}))});called});
        let mut stream=TcpStream::connect(address).unwrap();stream.write_all(request.as_bytes()).unwrap();
        let mut response=String::new();
        let result=stream.read_to_string(&mut response);
        // Rejected requests can leave an unread body; Windows may reset after
        // transmitting the complete denial response. Assert its actual status.
        assert!(result.is_ok() || result.as_ref().err().is_some_and(|error|error.kind()==std::io::ErrorKind::ConnectionReset));
        (response,task.join().unwrap())
    }
    #[test] fn browser_handoff_requires_nonce_origin_and_bounds() {
        let prefix="POST /session HTTP/1.1\r\nHost: HOST\r\nOrigin: chrome-extension://abcdefghijklmnopabcdefghijklmnop\r\n";
        let(response,called)=exchange(&format!("{prefix}X-MusicApp-Token: wrong\r\nContent-Length: 2\r\n\r\n[]"));assert!(response.contains("403"));assert!(!called);
        let(response,called)=exchange(&format!("{prefix}X-MusicApp-Token: secret\r\nContent-Length: 300000\r\n\r\n"));assert!(response.contains("413"));assert!(!called);
        let(response,called)=exchange(&format!("{prefix}X-MusicApp-Token: secret\r\nContent-Length: 2\r\n\r\n[]"));assert!(response.contains("200 OK"));assert!(called);
    }
}
