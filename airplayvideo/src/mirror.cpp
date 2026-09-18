#include "lab.hpp"
#include "stream.hpp"
#include <arpa/inet.h>
#include <poll.h>
#include <sodium.h>
#include <sys/socket.h>
#include <unistd.h>
namespace lab {
namespace {
class Timing {
  Socket socket_;
  std::jthread worker_;
  std::atomic<uint64_t> count_{0};

public:
  Timing(uint16_t port, const std::string &ip)
      : socket_(::socket(AF_INET, SOCK_DGRAM | SOCK_CLOEXEC, 0)) {
    require(socket_.fd() >= 0, "Timing socket allocation");
    sockaddr_in local{};
    local.sin_family = AF_INET;
    local.sin_port = htons(port);
    local.sin_addr.s_addr = INADDR_ANY;
    require(bind(socket_.fd(), reinterpret_cast<sockaddr *>(&local),
                 sizeof(local)) == 0,
            "Timing port unavailable");
    in_addr peer{};
    require(inet_pton(AF_INET, ip.c_str(), &peer) == 1,
            "Timing target invalid");
    worker_ = std::jthread([this, peer](std::stop_token stop) {
      while (!stop.stop_requested()) {
        pollfd p{socket_.fd(), POLLIN, 0};
        if (poll(&p, 1, 100) <= 0)
          continue;
        Bytes b(256);
        sockaddr_in from{};
        socklen_t size = sizeof(from);
        ssize_t n = recvfrom(socket_.fd(), b.data(), b.size(), 0,
                             reinterpret_cast<sockaddr *>(&from), &size);
        if (from.sin_addr.s_addr != peer.s_addr)
          continue;
        Bytes reply;
        uint64_t received = ntp_now();
        if (n == 48 && (b[0] & 7) == 3) {
          reply = Bytes(48);
          reply[0] = (b[0] & 0x38) | 4;
          reply[1] = 1;
          reply[2] = 2;
          reply[3] = 0xec;
          reply[12] = 'A';
          reply[13] = 'I';
          reply[14] = 'R';
          reply[15] = 'P';
          std::copy(b.begin() + 40, b.begin() + 48, reply.begin() + 24);
          be(reply, 32, received, 8);
          be(reply, 40, ntp_now(), 8);
        } else if (n == 32 && (b[1] & 0x7f) == 0x52) {
          reply = Bytes(32);
          reply[0] = 0x80;
          reply[1] = 0xd3;
          reply[2] = b[2];
          reply[3] = b[3];
          std::copy(b.begin() + 24, b.begin() + 32, reply.begin() + 8);
          be(reply, 16, received, 8);
          be(reply, 24, ntp_now(), 8);
        }
        if (!reply.empty() && sendto(socket_.fd(), reply.data(), reply.size(),
                                     0, reinterpret_cast<sockaddr *>(&from),
                                     sizeof(from)) == ssize_t(reply.size()))
          ++count_;
      }
    });
  }
  ~Timing() {
    worker_.request_stop();
    worker_.join();
  }
  uint64_t count() const { return count_; }
};
class Events {
  Channel channel_;
  std::jthread worker_;
  std::atomic<bool> healthy_{true};

public:
  Events(const std::string &ip, uint16_t port, std::span<const uint8_t> secret)
      : channel_(Socket::connect(ip, port)) {
    channel_.encrypt(secret, PAIR_CHANNEL_EVENTS);
    worker_ = std::jthread([this](std::stop_token stop) {
      while (!stop.stop_requested()) {
        try {
          auto m = channel_.read_message(90000);
          if (m.status != 0)
            continue;
          std::string response = "HTTP/1.1 200 OK\r\nContent-Length: 0\r\n";
          if (m.headers.contains("cseq"))
            response += "CSeq: " + m.headers.at("cseq") + "\r\n";
          response += "\r\n";
          channel_.write(bytes(response));
        } catch (...) {
          if (!stop.stop_requested())
            healthy_ = false;
          break;
        }
      }
    });
  }
  ~Events() {
    worker_.request_stop();
    channel_.shutdown();
    worker_.join();
  }
  bool healthy() const { return healthy_; }
};
} // namespace
void mirror(const Credentials &c, uint16_t timing_port, int seconds,
            std::atomic<bool> &stop, const Note &note) {
  require(seconds > 0 && seconds <= 60, "Demo duration out of range");
  Video video;
  auto first = video.frame(0);
  Rtsp control(c.receiver.address, c.receiver.port, c.sender_id);
  probe(control, c.receiver.name);
  auto secret = verify_pair(control, c, note);
  Timing timing(timing_port, c.receiver.address);
  std::unique_ptr<Events> events;
  const uint64_t stream_id = random_id();
  const std::string session = uuid();
  const std::string uri =
      "rtsp://" + c.receiver.address + "/" + std::to_string(random_id());
  // Public sender metadata is independent of all cryptographic key material.
  auto mac_bytes = random_bytes(6);
  mac_bytes[0] = 0x02;
  std::string mac;
  for (int i = 0; i < 6; ++i) {
    if (i)
      mac += ':';
    mac += hex(std::span(mac_bytes).subspan(i, 1));
  }
  bool setup = false;
  try {
    Json root = {
        {"deviceID", mac},           {"macAddress", mac},
        {"name", "C++ Hello World"}, {"model", "AirPlayMirrorLab"},
        {"sessionUUID", session},    {"isScreenMirroringSession", true},
        {"timingProtocol", "NTP"},   {"timingPort", timing_port},
        {"osName", "Linux"},         {"osVersion", "1.0"},
        {"sourceVersion", "409.16"}};
    auto result = control.plist("SETUP", uri, root);
    setup = true;
    uint64_t event_port = result.value("eventPort", uint64_t(0));
    require(event_port > 0 && event_port <= 65535,
            "Receiver did not offer an event channel");
    events = std::make_unique<Events>(c.receiver.address, event_port, secret);
    note("session_setup",
         {{"accepted", true},
          {"event_port", event_port},
          {"receiver_timing_port", result.value("timingPort", uint64_t(0))}});
    Json stream = {{"type", 110},
                   {"streamConnectionID", stream_id},
                   {"width", 1920},
                   {"height", 1080},
                   {"maxFPS", 30},
                   {"latencyMs", 100},
                   {"timestampInfo", Json::array({{{"name", "SubSu"}},
                                                  {{"name", "BePxT"}},
                                                  {{"name", "AfPxT"}},
                                                  {{"name", "BefEn"}},
                                                  {{"name", "EmEnc"}}})}};
    auto streams =
        control.plist("SETUP", uri, {{"streams", Json::array({stream})}});
    uint64_t data_port = 0, actual_id = stream_id;
    for (const auto &s : streams.value("streams", Json::array()))
      if (s.value("type", 0) == 110) {
        data_port = s.value("dataPort", uint64_t(0));
        actual_id = s.value("streamConnectionID", stream_id);
      }
    require(data_port > 0 && data_port <= 65535,
            "Receiver did not offer a mirroring channel");
    auto key = hkdf(secret, "DataStream-Salt" + std::to_string(actual_id),
                    "DataStream-Output-Encryption-Key");
    Socket data = Socket::connect(c.receiver.address, data_port);
    note("mirror_setup", {{"accepted", true},
                          {"data_port", data_port},
                          {"width", 1920},
                          {"height", 1080},
                          {"fps", 30}});
    auto record = control.request("RECORD", uri, {}, "", {{"Range", "npt=0-"}});
    require(record.status == 200,
            "RECORD rejected (RTSP " + std::to_string(record.status) + ")");
    note("record", {{"accepted", true}});
    auto start = Clock::now();
    uint64_t presentation = ntp_now() + (uint64_t(1) << 30);
    uint64_t sent = 0, wire_bytes = 0;
    auto config = mirror_header(first.avcc.size(), 1, presentation);
    config.insert(config.end(), first.avcc.begin(), first.avcc.end());
    data.write(config);
    for (int index = 0; index < seconds * 30 && !stop; ++index) {
      std::this_thread::sleep_until(
          start + std::chrono::nanoseconds(int64_t(index) * 1000000000 / 30));
      require(events->healthy(), "Receiver event channel closed");
      auto f = index == 0 ? std::move(first) : video.frame(index);
      auto packet = mirror_packet(
          f, key, index,
          presentation + (uint64_t(index) * (uint64_t(1) << 32)) / 30);
      data.write(packet);
      ++sent;
      wire_bytes += packet.size();
      if (index % 30 == 0)
        note("streaming", {{"frames_sent", sent},
                           {"wire_bytes", wire_bytes},
                           {"timing_replies", timing.count()},
                           {"seconds", index / 30}});
      if (index > 0 && index % 60 == 0) {
        auto feedback = control.request("POST", "/feedback");
        require(feedback.status == 200, "Receiver feedback rejected");
      }
    }
    auto teardown = control.request("TEARDOWN", uri);
    note("finished", {{"frames_sent", sent},
                      {"wire_bytes", wire_bytes},
                      {"timing_replies", timing.count()},
                      {"teardown_status", teardown.status},
                      {"stopped", bool(stop)}});
    setup = false;
    sodium_memzero(key.data(), key.size());
  } catch (...) {
    if (setup) {
      try {
        auto response = control.request("TEARDOWN", uri);
        note("teardown", {{"status", response.status}});
      } catch (...) {
        note("teardown", {{"connection_closed", true}});
      }
    }
    sodium_memzero(secret.data(), secret.size());
    throw;
  }
  sodium_memzero(secret.data(), secret.size());
}
} // namespace lab

namespace lab {
Json audio_timing_setup(int lead_ms) {
  require(lead_ms>=500&&lead_ms<=2000,"Invalid audio presentation lead");
  const int samples=lead_ms*44100/1000;
  // TimeAnnounce establishes the playout lead. SETUP describes its permitted
  // window, not an additional minimum queue to add to that lead.
  return {{"latencyMin",0},{"latencyMax",samples},{"usingScreen",true},{"isMedia",false}};
}
Json video_timing_setup(int lead_ms) {
  require(lead_ms>=500&&lead_ms<=2000,"Invalid video presentation lead");
  return {{"latencyMs",lead_ms}};
}
Bytes audio_sync_packet(uint64_t presentation_epoch,int64_t pts_us,
                        uint32_t timestamp,int lead_ms,bool first) {
  require(pts_us>=0,"Negative audio presentation timestamp");
  require(lead_ms>=500&&lead_ms<=2000,"Invalid audio presentation lead");
  Bytes sync(20); sync[0]=first?0x90:0x80; sync[1]=0xd4; be(sync,2,7,2);
  // Field 4 is the sample playing at the NTP instant; field 16 is the
  // corresponding packet being sent ahead. Both must describe the same clock.
  be(sync,4,timestamp-uint32_t(lead_ms*44100/1000),4);
  be(sync,8,presentation_epoch+ntp_delta(pts_us)-ntp_delta(uint64_t(lead_ms)*1000),8);
  be(sync,16,timestamp,4);
  return sync;
}
Bytes audio_packet(std::span<const uint8_t> pcm, std::span<const uint8_t> key,
                   uint64_t nonce, uint16_t sequence, uint32_t timestamp,
                   uint32_t ssrc, bool first) {
  require(pcm.size()==352*4,"PCM packet must contain 352 stereo samples");
  Bytes header(12); header[0]=0x80; header[1]=first?0xe0:0x60;
  be(header,2,sequence,2); be(header,4,timestamp,4); be(header,8,ssrc,4);
  auto cipher=seal(key,nonce,std::span(header).subspan(4,8),pcm);
  header.insert(header.end(),cipher.begin(),cipher.end());
  size_t end=header.size(); header.resize(end+8); le(header,end,nonce,8);
  return header;
}
namespace {
class AudioTransport {
  Socket control_{::socket(AF_INET,SOCK_DGRAM|SOCK_CLOEXEC,0)};
  Socket data_{::socket(AF_INET,SOCK_DGRAM|SOCK_CLOEXEC,0)};
  sockaddr_in peer_control_{},peer_data_{};
  Bytes key_;
  uint64_t nonce_=0;
  uint16_t sequence_=uint16_t(random_id());
  uint32_t origin_=uint32_t(random_id()),ssrc_=uint32_t(random_id());
  std::mutex mutex_;
  std::map<uint16_t,Bytes> history_;
  std::deque<uint16_t> order_;
  std::jthread worker_;
  int64_t last_sync_=-1000000;
  bool first_=true;
  uint64_t epoch_;
  int lead_ms_;
  int receiver_output_ms_=0;
public:
  explicit AudioTransport(uint16_t port,uint64_t epoch,int lead):epoch_(epoch),lead_ms_(lead) {
    require(control_.fd()>=0&&data_.fd()>=0,"Audio socket allocation");
    sockaddr_in local{}; local.sin_family=AF_INET; local.sin_port=htons(port); local.sin_addr.s_addr=INADDR_ANY;
    require(bind(control_.fd(),reinterpret_cast<sockaddr*>(&local),sizeof(local))==0,"Audio control port unavailable");
  }
  void setup(Rtsp &rtsp,const std::string &uri,const Credentials &c,uint16_t port) {
    key_=random_bytes(32);
    Json stream={{"type",96},{"audioFormat",0x800},{"audioMode","default"},{"ct",1},
      {"sr",44100},{"spf",352},{"controlPort",port},
      {"shk",Json::binary(key_)},
      {"streamConnectionID",random_id()},{"supportsDynamicStreamID",false}};
    stream.update(audio_timing_setup(lead_ms_));
    auto reply=rtsp.plist("SETUP",uri,{{"streams",Json::array({stream})}});
    int cp=0,dp=0;
    for(const auto &s:reply.value("streams",Json::array())) if(s.value("type",0)==96) {
      cp=s.value("controlPort",0);dp=s.value("dataPort",0);
      receiver_output_ms_=s.value("arrivalToRenderLatencyMs",0);
    }
    require(cp>0&&cp<=65535&&dp>0&&dp<=65535,"Receiver did not offer PCM audio transport");
    peer_control_.sin_family=peer_data_.sin_family=AF_INET;
    require(inet_pton(AF_INET,c.receiver.address.c_str(),&peer_control_.sin_addr)==1,"Audio receiver address");
    peer_data_.sin_addr=peer_control_.sin_addr;
    peer_control_.sin_port=htons(cp); peer_data_.sin_port=htons(dp);
    worker_=std::jthread([this](std::stop_token stop) {
      while(!stop.stop_requested()) {
        pollfd p{control_.fd(),POLLIN,0}; if(poll(&p,1,100)<=0) continue;
        Bytes request(256); sockaddr_in from{}; socklen_t len=sizeof(from);
        auto n=recvfrom(control_.fd(),request.data(),request.size(),0,reinterpret_cast<sockaddr*>(&from),&len);
        if(n<8 || from.sin_addr.s_addr!=peer_control_.sin_addr.s_addr || from.sin_port!=peer_control_.sin_port || (request[1]&0x7f)!=0x55) continue;
        uint16_t first=uint16_t(request[4])<<8|request[5];
        int count=std::min(32,int(uint16_t(request[6])<<8|request[7]));
        std::lock_guard lock(mutex_);
        for(int i=0;i<count;++i) {
          auto found=history_.find(uint16_t(first+i)); if(found==history_.end()) continue;
          Bytes reply={0x80,0xd6,found->second[2],found->second[3]}; reply.insert(reply.end(),found->second.begin(),found->second.end());
          sendto(control_.fd(),reply.data(),reply.size(),0,reinterpret_cast<sockaddr*>(&peer_control_),sizeof(peer_control_));
        }
      }
    });
  }
  ~AudioTransport() { if(worker_.joinable()) {worker_.request_stop();worker_.join();} sodium_memzero(key_.data(),key_.size()); }
  Json timing() const {
    return {{"codec","pcm"},{"sample_rate",44100},{"samples_per_packet",352},
      {"latency_min_samples",0},{"latency_max_samples",lead_ms_*44100/1000},
      {"receiver_output_latency_ms",receiver_output_ms_},{"is_media",false},{"using_screen",true}};
  }
  void send(const MediaPacket &p) {
    uint32_t timestamp=origin_+uint32_t((uint64_t(p.pts_us)*44100)/1000000);
    if(p.pts_us-last_sync_>=1000000) {
      auto sync=audio_sync_packet(epoch_,p.pts_us,timestamp,lead_ms_,first_);
      require(sendto(control_.fd(),sync.data(),sync.size(),0,reinterpret_cast<sockaddr*>(&peer_control_),sizeof(peer_control_))==ssize_t(sync.size()),"Audio synchronization send failed");
      last_sync_=p.pts_us;
    }
    auto packet=audio_packet(p.pcm,key_,nonce_++,sequence_,timestamp,ssrc_,first_);
    { std::lock_guard lock(mutex_); history_[sequence_]=packet; order_.push_back(sequence_); if(order_.size()>512) {history_.erase(order_.front());order_.pop_front();} }
    require(sendto(data_.fd(),packet.data(),packet.size(),0,reinterpret_cast<sockaddr*>(&peer_data_),sizeof(peer_data_))==ssize_t(packet.size()),"Audio send failed");
    ++sequence_; first_=false;
  }
};
}

void mirror_stream(const Credentials &c,Media &media,uint16_t timing_port,
                   uint16_t control_port,std::atomic<bool> &stop,const Note &note) {
  Rtsp control(c.receiver.address,c.receiver.port,c.sender_id);
  auto receiver=probe(control,c.receiver.name);
  require(receiver.value("deviceID",c.receiver.device_id)==c.receiver.device_id,"Receiver identity changed; saved pairing retained");
  auto secret=verify_pair(control,c,note);
  Timing timing(timing_port,c.receiver.address);
  auto uri="rtsp://"+c.receiver.address+"/"+std::to_string(random_id());
  Bytes key;
  bool setup=false;
  try {
    std::string mac="02:"+hex(random_bytes(1))+":"+hex(random_bytes(1))+":"+hex(random_bytes(1))+":"+hex(random_bytes(1))+":"+hex(random_bytes(1));
    auto root=control.plist("SETUP",uri,{{"deviceID",mac},{"macAddress",mac},{"name","AirplayVideo"},{"model","AirplayVideo"},{"sessionUUID",uuid()},{"isScreenMirroringSession",true},{"timingProtocol","NTP"},{"timingPort",timing_port},{"osName","Linux"},{"osVersion","1.0"},{"sourceVersion","409.16"}});
    setup=true;
    int ep=root.value("eventPort",0); require(ep>0&&ep<=65535,"Receiver event port unavailable");
    Events events(c.receiver.address,ep,secret);
    int width=media.config.at("width"),height=media.config.at("height"),fps=media.config.at("fps");
    uint64_t stream_id=random_id();
    Json stream={{"type",110},{"streamConnectionID",stream_id},{"width",width},{"height",height},{"maxFPS",fps},{"timestampInfo",Json::array({{{"name","SubSu"}},{{"name","BePxT"}},{{"name","AfPxT"}},{{"name","BefEn"}},{{"name","EmEnc"}}})}};
    stream.update(video_timing_setup(media.config.value("latency_ms",1500)));
    auto reply=control.plist("SETUP",uri,{{"streams",Json::array({stream})}});
    int dp=0;
    for(const auto &s:reply.value("streams",Json::array())) if(s.value("type",0)==110) {dp=s.value("dataPort",0);stream_id=s.value("streamConnectionID",stream_id);}
    require(dp>0&&dp<=65535,"Receiver mirroring port unavailable");
    key=hkdf(secret,"DataStream-Salt"+std::to_string(stream_id),"DataStream-Output-Encryption-Key");
    Socket data=Socket::connect(c.receiver.address,dp);
    std::unique_ptr<AudioTransport> audio;
    if(media.config.value("audio",true)) {
      audio=std::make_unique<AudioTransport>(control_port,media.epoch_ntp,media.config.value("latency_ms",1500));
      audio->setup(control,uri,c,control_port);
    }
    auto record=control.request("RECORD",uri,{},"",{{"Range","npt=0-"}});
    require(record.status==200,"Receiver rejected playback");
    auto volume=control.request("SET_PARAMETER",uri,bytes("volume: 0.000000\r\n"),"text/parameters");
    // Request digital unity gain for this audio stream.
    require(!audio||volume.status==200,"Receiver rejected audio gain");
    auto subscription=media.subscribe();
    note("connecting",{{"video",true},{"audio",bool(audio)}});
    uint64_t frames=0,audio_packets=0,nonce=0,wire_bytes=0;
    int64_t first_pts=-1,last_video=-1;
    int64_t video_age=0,audio_age=0,video_queue=0,audio_queue=0;
    auto heartbeat=Clock::now(),metrics=Clock::now(),last_packet=Clock::now();
    Bytes last_config;
    while(!stop && !media.failed()) {
      auto p=subscription->next(stop);
      if(!p) {
        require(Clock::now()-last_packet<std::chrono::seconds(8),"Media source stopped producing frames");
        continue;
      }
      last_packet=Clock::now();
      if(first_pts<0) {
        if(p->audio||!p->video.keyframe) continue;
        first_pts=p->pts_us;
      }
      if(p->pts_us<first_pts) continue;
      require(events.healthy(),"Receiver closed the event channel");
      int64_t age=std::chrono::duration_cast<std::chrono::microseconds>(Clock::now()-media.epoch).count()-p->pts_us;
      int64_t queue=std::chrono::duration_cast<std::chrono::microseconds>(Clock::now()-media.epoch).count()-p->available_us;
      require(age<int64_t(media.config.value("latency_ms",1500))*1000,"Receiver is too far behind; playback stopped");
      if(p->audio) { if(audio) {audio->send(*p);++audio_packets;audio_age=age;audio_queue=queue;} }
      else {
        video_age=age;video_queue=queue;
        require(p->pts_us>last_video,"Nonmonotonic video timestamp"); last_video=p->pts_us;
        uint64_t presentation=media.epoch_ntp+ntp_delta(p->pts_us);
        if(last_config!=p->video.avcc) {
          auto config=mirror_header(p->video.avcc.size(),1,presentation,width,height);
          config.insert(config.end(),p->video.avcc.begin(),p->video.avcc.end()); data.write(config); last_config=p->video.avcc;
        }
        auto packet=mirror_packet(p->video,key,nonce++,presentation,width,height);
        data.write(packet); ++frames; wire_bytes+=packet.size();
      }
      if(Clock::now()-metrics>=std::chrono::seconds(1)) {
        note("streaming",{{"video_frames",frames},{"audio_packets",audio_packets},{"wire_bytes",wire_bytes},{"timing_replies",timing.count()},
          {"video_age_us",video_age},{"audio_age_us",audio_age},{"video_queue_us",video_queue},{"audio_queue_us",audio_queue},
          {"video_setup_latency_ms",media.config.at("latency_ms")},{"presentation_lead_ms",media.config.at("latency_ms")},
          {"audio_timing",audio?audio->timing():Json(nullptr)}}); metrics=Clock::now();
      }
      if(Clock::now()-heartbeat>=std::chrono::seconds(2)) {
        require(control.request("POST","/feedback").status==200,"Receiver feedback rejected"); heartbeat=Clock::now();
      }
    }
    subscription->close();
    auto response=control.request("TEARDOWN",uri); setup=false;
    note("stopped",{{"teardown_status",response.status},{"video_frames",frames},{"audio_packets",audio_packets}});
  } catch(...) {
    if(setup) try {control.request("TEARDOWN",uri);} catch(...) {}
    sodium_memzero(secret.data(),secret.size()); sodium_memzero(key.data(),key.size()); throw;
  }
  sodium_memzero(secret.data(),secret.size()); sodium_memzero(key.data(),key.size());
}
}
