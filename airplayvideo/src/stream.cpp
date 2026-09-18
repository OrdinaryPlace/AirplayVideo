#include "stream.hpp"
#include <iostream>
#include <poll.h>
#include <regex>
#include <unistd.h>
namespace lab {
int run_stream(const Json &config,const std::filesystem::path &directory,
               std::atomic<bool> &stop,const Note &note) {
  Media media(config,stop,note);
  struct Session {
    std::atomic<bool> stop{false};
    std::atomic<bool> finished{false};
    int slot=0;
    std::jthread thread;
    ~Session() { stop=true; if(thread.joinable()) thread.join(); }
  };
  std::map<std::string,std::unique_ptr<Session>> sessions;
  media.start();
  std::jthread commands([&](std::stop_token ending) {
    std::string buffer;
    while(!ending.stop_requested()&&!stop) {
      pollfd fd{STDIN_FILENO,POLLIN,0};
      if(poll(&fd,1,100)<=0) continue;
      char data[4096]; ssize_t count=read(STDIN_FILENO,data,sizeof(data));
      if(count<=0) {stop=true;break;}
      buffer.append(data,count);
      if(buffer.size()>16384) {stop=true;break;}
      size_t end;
      while((end=buffer.find('\n'))!=std::string::npos) {
        auto line=buffer.substr(0,end); buffer.erase(0,end+1);
        try {
          auto command=Json::parse(line);
          std::string action=command.at("action");
          if(action=="stop") {stop=true;break;}
          std::string id=command.at("id");
          require(std::regex_match(id,std::regex("[0-9a-f]{32}")),"Invalid receiver identifier");
          if(action=="remove") {
            sessions.erase(id); note("receiver_removed",{{"id",id}}); continue;
          }
          require(action=="add","Unknown stream command");
          if(sessions.contains(id)&&!sessions.at(id)->finished) continue;
          sessions.erase(id);
          int slot=command.at("slot"); require(slot>=0&&slot<8,"Invalid receiver slot");
          for(const auto &[other,s]:sessions) require(s->slot!=slot,"Receiver slot already in use");
          auto credentials=Credentials::from_json(Json::parse(private_read(directory/(id+".json"))));
          auto session=std::make_unique<Session>(); session->slot=slot; auto *ptr=session.get();
          ptr->thread=std::jthread([&,ptr,id,credentials,slot] {
            auto receiver_note=[&](const std::string &stage,const Json &details) {
              auto fields=details; fields["id"]=id; note(stage,fields);
            };
            try {
              mirror_stream(credentials,media,18200+slot,18208+slot,ptr->stop,receiver_note);
            } catch(const Json::exception &) {
              receiver_note("receiver_error",{{"message","Unexpected receiver response"}});
            } catch(const std::exception &e) {
              receiver_note("receiver_error",{{"message",e.what()}});
            }
            ptr->finished=true;
          });
          sessions.emplace(id,std::move(session));
        } catch(const Json::exception &) {note("command_error",{{"message","Invalid command structure"}});}
          catch(const std::exception &e) {note("command_error",{{"message",e.what()}});}
      }
    }
  });
  while(!stop&&!media.failed()&&!media.completed()) std::this_thread::sleep_for(std::chrono::milliseconds(50));
  stop=true; commands.request_stop(); commands.join();
  for(auto &[id,s]:sessions) s->stop=true;
  media.close(); sessions.clear();
  if(media.completed()) note("source_finished",Json::object());
  note("source_stopped",{{"failed",media.failed()}});
  return media.failed()?1:0;
}
}
