#include "stream.hpp"
#include <csignal>
#include <httplib.h>
#include <iostream>
#include <optional>
#include <regex>
#include <sodium.h>
#include <sys/stat.h>
extern "C" {
#include <libavdevice/avdevice.h>
#include <libavutil/log.h>
}
using namespace lab;
namespace {
std::atomic<bool> exiting{false};
static_assert(std::atomic<bool>::is_always_lock_free);
void request_exit(int) { exiting.store(true,std::memory_order_relaxed); }
std::mutex output_mutex;
void event(const std::string &name,const Json &details) {
  std::lock_guard lock(output_mutex);
  std::cout<<Json({{"event",name},{"details",details}}).dump()<<std::endl;
}
const Note quiet=[](const std::string &,const Json &){};
void validate_id(const std::string &id) {require(std::regex_match(id,std::regex("[0-9a-f]{32}")),"Invalid receiver identifier");}
class Pairings {
  std::filesystem::path directory_;
  std::mutex mutex_;
  std::unique_ptr<Rtsp> pending_;
  Receiver receiver_;
  std::string sender_,id_;
  Clock::time_point expires_;
public:
  explicit Pairings(std::filesystem::path p):directory_(std::move(p)) {
    std::filesystem::create_directories(directory_); chmod(directory_.c_str(),0700);
  }
  Json saved() {
    std::lock_guard lock(mutex_); Json result=Json::array();
    for(const auto &file:std::filesystem::directory_iterator(directory_)) {
      if(file.path().extension()!=".json") continue;
      auto id=file.path().stem().string(); validate_id(id);
      auto c=Credentials::from_json(Json::parse(private_read(file.path())));
      auto receiver=c.receiver.json(); receiver["id"]=id; result.push_back(receiver);
    }
    return result;
  }
  Json inspect(const Json &input) {
    std::string address=input.at("address"); int port=input.value("port",7000);
    require(port>0&&port<=65535,"Invalid receiver port");
    Rtsp rtsp(address,port,uuid());
    auto info=probe(rtsp,input.value("name",""));
    Receiver receiver{info.value("name",""),address,info.value("deviceID",""),info.value("model",""),info.value("sourceVersion",""),uint16_t(port)};
    require(!receiver.name.empty()&&!receiver.device_id.empty(),"Receiver did not identify itself");
    return receiver.json();
  }
  Json start(const Json &input) {
    std::lock_guard lock(mutex_);
    require(!pending_,"Finish or cancel the current pairing first");
    id_=input.at("id"); validate_id(id_);
    require(!std::filesystem::exists(directory_/(id_+".json")),"A pairing is already saved for this TV");
    auto found=inspect(input); receiver_={found.at("name"),found.at("address"),found.at("device_id"),found.at("model"),found.at("version"),found.at("port")};
    sender_=uuid(); auto connection=std::make_unique<Rtsp>(receiver_.address,receiver_.port,sender_);
    probe(*connection,receiver_.name);
    auto response=connection->request("POST","/pair-pin-start",{},"",{{"X-Apple-HKP","3"}});
    require(response.status==200,"The TV rejected the PIN request");
    pending_=std::move(connection); expires_=Clock::now()+std::chrono::minutes(3);
    return {{"id",id_},{"name",receiver_.name},{"waiting_for_pin",true}};
  }
  Json finish(std::string pin) {
    std::lock_guard lock(mutex_);
    require(bool(pending_),"Request a PIN on the TV first");
    if(Clock::now()>expires_) {pending_.reset();throw std::runtime_error("PIN expired; request another code");}
    try {
      auto c=pair_with_pin(*pending_,receiver_,sender_,pin,quiet);
      sodium_memzero(pin.data(),pin.size());
      auto secret=verify_pair(*pending_,c,quiet); sodium_memzero(secret.data(),secret.size());
      probe(*pending_,receiver_.name);
      private_write_new(directory_/(id_+".json"),c.json().dump());
      pending_.reset();
      auto result=receiver_.json();result["id"]=id_;return result;
    } catch(...) {sodium_memzero(pin.data(),pin.size());pending_.reset();throw;}
  }
  void cancel() {std::lock_guard lock(mutex_);pending_.reset();}
};
}
int main(int argc,char **argv) {
  try {
    require(sodium_init()>=0,"Crypto initialization failed");
    // Never print libav input addresses, paths, device metadata, or auth errors.
    av_log_set_level(AV_LOG_QUIET); avdevice_register_all();
    std::signal(SIGTERM,request_exit);std::signal(SIGINT,request_exit);
    std::string mode="server",argument;
    std::filesystem::path data="/data/receivers";
    int port=8098;
    for(int i=1;i<argc;++i) {
      std::string arg=argv[i];
      auto value=[&]{require(i+1<argc,"Missing argument");return std::string(argv[++i]);};
      if(arg=="--data") data=value();
      else if(arg=="--port") port=std::stoi(value());
      else if(arg=="--stream") {mode="stream";argument=value();}
      else if(arg=="--capabilities") mode="capabilities";
      else if(arg=="--discover") mode="discover";
      else if(arg=="--sample") {mode="sample";argument=value();}
      else throw std::runtime_error("Unknown engine argument");
    }
    if(mode=="stream") return run_stream(Json::parse(private_read(argument)),data,exiting,event);
    if(mode=="capabilities") {std::cout<<media_capabilities().dump()<<std::endl;return 0;}
    if(mode=="discover") {Json list=Json::array();for(const auto &r:discover())list.push_back(r.json());std::cout<<list.dump()<<std::endl;return 0;}
    if(mode=="sample") {write_sample(argument,3);return 0;}
    require(port>0&&port<=65535,"Invalid API port");
    Pairings pairings(data);
    httplib::Server server;server.set_payload_max_length(8192);server.set_read_timeout(5);server.set_write_timeout(5);
    server.set_pre_routing_handler([](const auto &req,auto &res) {
      res.set_header("Cache-Control","no-store");
      if(req.remote_addr!="127.0.0.1"||(req.method=="POST"&&(req.get_header_value("X-AirplayVideo")!="1"||!req.get_header_value("Content-Type").starts_with("application/json")))) {
        res.status=403;return httplib::Server::HandlerResponse::Handled;
      }
      return httplib::Server::HandlerResponse::Unhandled;
    });
    auto route=[&](std::string path,std::function<Json(const Json&)> function) {
      server.Post(path,[function](const auto &req,auto &res) {
        try {res.set_content(function(Json::parse(req.body)).dump(),"application/json");}
        catch(const Json::exception &) {res.status=400;res.set_content("{\"error\":\"Invalid request or saved data\"}","application/json");}
        catch(const std::exception &e) {res.status=409;res.set_content(Json({{"error",e.what()}}).dump(),"application/json");}
      });
    };
    route("/receivers",[&](const auto &){return pairings.saved();});
    route("/discover",[&](const auto &){Json r=Json::array();for(const auto &d:discover())r.push_back(d.json());return r;});
    route("/probe",[&](const auto &j){return pairings.inspect(j);});
    route("/pair/start",[&](const auto &j){return pairings.start(j);});
    route("/pair/finish",[&](const auto &j){return pairings.finish(j.at("pin"));});
    route("/pair/cancel",[&](const auto &){pairings.cancel();return Json::object();});
    route("/capabilities",[](const auto &){return media_capabilities();});
    server.Get("/health",[](const auto &,auto &res){res.set_content("ready","text/plain");});
    require(server.bind_to_port("127.0.0.1",port),"Engine API port unavailable");
    std::jthread monitor([&](std::stop_token stopping){while(!stopping.stop_requested()){if(exiting&&server.is_running()){server.stop();break;}std::this_thread::sleep_for(std::chrono::milliseconds(50));}});
    event("engine_ready",{});
    bool result=server.listen_after_bind();monitor.request_stop();
    require(result||exiting,"Engine API failed");return 0;
  } catch(const Json::exception &) {event("fatal",{{"message","Invalid saved configuration; files preserved"}});return 1;}
    catch(const std::exception &e) {event("fatal",{{"message",e.what()}});return 1;}
}
